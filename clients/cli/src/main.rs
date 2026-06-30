//! transcribbler CLI client (ADR-0017): a single binary that talks the ADR-0018
//! wire. `transcribe` submits a file and streams progress; `render` formats a
//! local IR; `profiles`/`version` introspect the server.

mod client;
mod ir;
mod render;

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand};

use client::{Endpoint, SubmitOpts};

/// The wire major this client speaks (ADR-0018 `/v1`).
const WIRE_MAJOR: &str = "1";
/// The IR schema version the codegen'd types were built against.
const IR_SCHEMA: &str = "0.1";

#[derive(Parser)]
#[command(
    name = "transcribbler",
    version,
    about = "transcribbler client — talks the ADR-0018 wire"
)]
struct Cli {
    #[command(flatten)]
    conn: ConnOpts,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Args)]
struct ConnOpts {
    /// Unix socket path (default: $XDG_RUNTIME_DIR/transcribbler.sock)
    #[arg(long, global = true)]
    socket: Option<PathBuf>,
    /// TCP base URL (e.g. http://cube:8080); overrides --socket
    #[arg(long, global = true)]
    url: Option<String>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Transcribe a file over the wire → md/vtt/json
    Transcribe {
        audio: PathBuf,
        /// Compute profile name (server-side allowlist); auto-selected if omitted
        #[arg(short, long)]
        profile: Option<String>,
        #[arg(short, long, default_value = "json")]
        format: String,
        /// Write here (default: stdout)
        #[arg(short, long)]
        output: Option<PathBuf>,
        #[arg(long)]
        no_diarize: bool,
        #[arg(long)]
        no_canon: bool,
        /// Initial prompt to bias ASR spelling (names, jargon)
        #[arg(long)]
        prompt: Option<String>,
    },
    /// Render a local Canonical IR (.json) to md/vtt/json
    Render {
        ir: PathBuf,
        #[arg(short, long, default_value = "md")]
        format: String,
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
    /// List the server's compute profiles
    Profiles,
    /// Show the server's wire/IR versions + capabilities
    Version,
}

fn endpoint(c: &ConnOpts) -> Result<Endpoint> {
    if let Some(url) = &c.url {
        if url.starts_with("https://") {
            // No TLS client yet (ADR-0020); accepting https:// and speaking
            // cleartext would be a silent downgrade. Fail loudly instead.
            bail!("remote TLS is not supported yet (ADR-0020); tunnel over SSH and use http://");
        }
        let rest = url
            .strip_prefix("http://")
            .unwrap_or(url)
            .trim_end_matches('/');
        let (host, port) = match rest.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().context("parse port from --url")?),
            None => (rest.to_string(), 80),
        };
        Ok(Endpoint::Tcp { host, port })
    } else {
        let sock = c.socket.clone().unwrap_or_else(default_socket);
        Ok(Endpoint::Unix(sock))
    }
}

fn default_socket() -> PathBuf {
    let base = std::env::var("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("transcribbler.sock")
}

fn emit(text: &str, output: &Option<PathBuf>) -> Result<()> {
    match output {
        Some(path) => {
            std::fs::write(path, text).with_context(|| format!("write {}", path.display()))?;
            eprintln!("wrote {}", path.display());
        }
        None => {
            std::io::stdout().write_all(text.as_bytes())?;
        }
    }
    Ok(())
}

async fn run() -> Result<i32> {
    let cli = Cli::parse();
    let ep = endpoint(&cli.conn)?;

    match cli.cmd {
        Cmd::Version => {
            let v = client::get_version(&ep).await?;
            println!("wire_version    : {}", v.wire_version);
            println!("ir_schema_version: {}", v.ir_schema_version);
            println!("capabilities    : {}", v.capabilities);
            Ok(0)
        }
        Cmd::Profiles => {
            for p in client::get_profiles(&ep).await? {
                let stage = |s: &Option<String>| s.clone().unwrap_or_else(|| "-".into());
                println!(
                    "{:<16} asr={} diar={} llm={}",
                    p.name,
                    stage(&p.asr),
                    stage(&p.diar),
                    stage(&p.llm)
                );
            }
            Ok(0)
        }
        Cmd::Render { ir, format, output } => {
            let raw =
                std::fs::read_to_string(&ir).with_context(|| format!("read {}", ir.display()))?;
            let value = serde_json::from_str(&raw).context("parse IR json")?;
            emit(&render::render(&value, &format)?, &output)?;
            Ok(0)
        }
        Cmd::Transcribe {
            audio,
            profile,
            format,
            output,
            no_diarize,
            no_canon,
            prompt,
        } => {
            check_version(&ep).await;
            let bytes = tokio::fs::read(&audio)
                .await
                .with_context(|| format!("read {}", audio.display()))?;
            let filename = audio
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("audio");
            let created = client::submit_job(
                &ep,
                filename,
                bytes,
                &SubmitOpts {
                    profile: profile.as_deref().unwrap_or(""),
                    diarize: !no_diarize,
                    canon: !no_canon,
                    prompt: prompt.as_deref(),
                },
            )
            .await?;
            eprintln!("job {} ({})", created.id, created.status);

            let mut open_line = false;
            let terminal = client::stream_events(&ep, &created.id, |ev| {
                open_line = render_progress(ev);
            })
            .await?;
            if open_line {
                eprintln!();
            }

            match terminal.event.as_str() {
                "done" => {
                    let job = client::get_job(&ep, &created.id).await?;
                    let ir_value = job.ir.context("server reported done but returned no IR")?;
                    emit(&render::render(&ir_value, &format)?, &output)?;
                    Ok(0)
                }
                "error" => {
                    let code = terminal
                        .data
                        .get("code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("internal");
                    let msg = terminal
                        .data
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    eprintln!("error [{code}]: {msg}");
                    Ok(1)
                }
                other => {
                    eprintln!("job {other}");
                    Ok(1)
                }
            }
        }
    }
}

/// Render a non-terminal SSE event to stderr; returns whether the line is left
/// open (a `\r` progress write with no newline) so the caller can close it.
fn render_progress(ev: &client::SseEvent) -> bool {
    match ev.event.as_str() {
        "queued" => {
            let ahead = ev.data.get("ahead").and_then(|v| v.as_i64()).unwrap_or(0);
            eprint!("\r  queued ({ahead} ahead) ");
            true
        }
        "progress" => {
            let stage = ev.data.get("stage").and_then(|v| v.as_str()).unwrap_or("?");
            let pct = ev.data.get("pct").and_then(|v| v.as_i64()).unwrap_or(0);
            eprint!("\r  {stage:<5} {pct:3}%");
            true
        }
        "paused" => {
            eprintln!("\n  paused (yielding to live mode)");
            false
        }
        "resumed" => {
            eprintln!("  resumed");
            false
        }
        _ => false,
    }
}

/// Soft version handshake (ADR-0018): refuse an unknown wire major, warn on an
/// IR schema the codegen'd types weren't built for. Best-effort — a server too
/// old to answer /version still gets a try.
async fn check_version(ep: &Endpoint) {
    let Ok(v) = client::get_version(ep).await else {
        return;
    };
    let major = v.wire_version.split('.').next().unwrap_or(&v.wire_version);
    if major != WIRE_MAJOR {
        eprintln!(
            "warning: server wire_version {} != client {WIRE_MAJOR}; behavior may differ",
            v.wire_version
        );
    }
    if v.ir_schema_version != IR_SCHEMA {
        eprintln!(
            "warning: server ir_schema_version {} != client {IR_SCHEMA}; IR may not parse",
            v.ir_schema_version
        );
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(0) => ExitCode::SUCCESS,
        Ok(code) => ExitCode::from(code as u8),
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

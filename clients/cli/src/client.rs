//! The ADR-0018 wire client: one HTTP path over a Unix socket (local default)
//! or TCP (remote). The transport differs only at connect; everything above is
//! the same HTTP/1.1. Connections are one-shot (a CLI invocation makes a handful
//! of requests), so there's no pool — just connect, handshake, send.

use std::path::PathBuf;

use anyhow::{anyhow, bail, Context, Result};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::client::conn::http1;
use hyper::{Method, Request, Response};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use serde_json::Value;
use tokio::net::{TcpStream, UnixStream};

#[derive(Clone, Debug)]
pub enum Endpoint {
    Unix(PathBuf),
    Tcp { host: String, port: u16 },
}

impl Endpoint {
    fn host_header(&self) -> String {
        match self {
            Endpoint::Unix(_) => "localhost".to_string(),
            Endpoint::Tcp { host, port } => format!("{host}:{port}"),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct JobCreated {
    pub id: String,
    pub status: String,
}

#[derive(Debug, Deserialize)]
pub struct JobState {
    // Terminal state is taken from the SSE stream; this fetch is only for the IR.
    #[serde(default)]
    pub ir: Option<Value>,
}

#[derive(Debug, Deserialize)]
pub struct ProfileInfo {
    pub name: String,
    pub asr: Option<String>,
    pub diar: Option<String>,
    pub llm: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct VersionInfo {
    pub wire_version: String,
    pub ir_schema_version: String,
    pub capabilities: Value,
}

/// One SSE event: its `event:` type and parsed `data:` JSON payload.
#[derive(Debug)]
pub struct SseEvent {
    pub event: String,
    pub data: Value,
}

pub const TERMINAL: [&str; 3] = ["done", "error", "canceled"];

async fn connect(ep: &Endpoint) -> Result<http1::SendRequest<Full<Bytes>>> {
    let io = match ep {
        Endpoint::Unix(path) => {
            let stream = UnixStream::connect(path)
                .await
                .with_context(|| format!("connect unix socket {}", path.display()))?;
            TokioIo::new(Box::new(stream) as Box<dyn Stream>)
        }
        Endpoint::Tcp { host, port } => {
            let stream = TcpStream::connect((host.as_str(), *port))
                .await
                .with_context(|| format!("connect tcp {host}:{port}"))?;
            TokioIo::new(Box::new(stream) as Box<dyn Stream>)
        }
    };
    let (sender, conn) = http1::handshake(io).await.context("http handshake")?;
    tokio::spawn(async move {
        let _ = conn.await;
    });
    Ok(sender)
}

// A tiny object-safe alias so both stream types flow through one connector.
trait Stream: tokio::io::AsyncRead + tokio::io::AsyncWrite + Send + Unpin {}
impl<T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Send + Unpin> Stream for T {}

async fn send(ep: &Endpoint, req: Request<Full<Bytes>>) -> Result<Response<hyper::body::Incoming>> {
    let mut sender = connect(ep).await?;
    sender.send_request(req).await.context("send request")
}

fn build(
    ep: &Endpoint,
    method: Method,
    path: &str,
    content_type: Option<&str>,
    body: Bytes,
) -> Result<Request<Full<Bytes>>> {
    let mut b = Request::builder()
        .method(method)
        .uri(path)
        .header("host", ep.host_header());
    if let Some(ct) = content_type {
        b = b.header("content-type", ct);
    }
    b.body(Full::new(body)).context("build request")
}

async fn read_json<T: for<'de> Deserialize<'de>>(
    resp: Response<hyper::body::Incoming>,
) -> Result<T> {
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .context("read body")?
        .to_bytes();
    if !status.is_success() {
        bail!(
            "server returned {status}: {}",
            String::from_utf8_lossy(&bytes)
        );
    }
    serde_json::from_slice(&bytes).context("decode response json")
}

pub async fn get_version(ep: &Endpoint) -> Result<VersionInfo> {
    read_json(
        send(
            ep,
            build(ep, Method::GET, "/v1/version", None, Bytes::new())?,
        )
        .await?,
    )
    .await
}

pub async fn get_profiles(ep: &Endpoint) -> Result<Vec<ProfileInfo>> {
    read_json(
        send(
            ep,
            build(ep, Method::GET, "/v1/profiles", None, Bytes::new())?,
        )
        .await?,
    )
    .await
}

pub async fn get_job(ep: &Endpoint, id: &str) -> Result<JobState> {
    read_json(
        send(
            ep,
            build(
                ep,
                Method::GET,
                &format!("/v1/jobs/{id}"),
                None,
                Bytes::new(),
            )?,
        )
        .await?,
    )
    .await
}

pub struct SubmitOpts<'a> {
    pub profile: &'a str,
    pub diarize: bool,
    pub canon: bool,
    pub prompt: Option<&'a str>,
}

pub async fn submit_job(
    ep: &Endpoint,
    filename: &str,
    audio: Vec<u8>,
    opts: &SubmitOpts<'_>,
) -> Result<JobCreated> {
    let boundary = "----transcribblerFormBoundary7a3b";
    let mut fields: Vec<(String, String)> = vec![
        ("profile".into(), opts.profile.to_string()),
        ("diarize".into(), opts.diarize.to_string()),
        ("canon".into(), opts.canon.to_string()),
    ];
    if let Some(p) = opts.prompt {
        fields.push(("prompt".into(), p.to_string()));
    }
    let mut body: Vec<u8> = Vec::with_capacity(audio.len() + 512);
    for (name, val) in &fields {
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n").as_bytes(),
        );
        body.extend_from_slice(val.as_bytes());
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        format!("Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n")
            .as_bytes(),
    );
    body.extend_from_slice(&audio);
    body.extend_from_slice(b"\r\n");
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());

    let ct = format!("multipart/form-data; boundary={boundary}");
    let resp = send(
        ep,
        build(ep, Method::POST, "/v1/jobs", Some(&ct), Bytes::from(body))?,
    )
    .await?;
    read_json(resp).await
}

/// Stream a job's SSE events, invoking `on_event` for each, until a terminal
/// event (done/error/canceled), which is returned.
pub async fn stream_events(
    ep: &Endpoint,
    id: &str,
    mut on_event: impl FnMut(&SseEvent),
) -> Result<SseEvent> {
    let resp = send(
        ep,
        build(
            ep,
            Method::GET,
            &format!("/v1/jobs/{id}/events"),
            None,
            Bytes::new(),
        )?,
    )
    .await?;
    if !resp.status().is_success() {
        let bytes = resp.into_body().collect().await?.to_bytes();
        bail!("events stream failed: {}", String::from_utf8_lossy(&bytes));
    }
    let mut body = resp.into_body();
    let mut buf = String::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.context("read sse frame")?;
        let Some(chunk) = frame.data_ref() else {
            continue;
        };
        buf.push_str(&String::from_utf8_lossy(chunk));
        while let Some(idx) = buf.find("\n\n") {
            let raw: String = buf.drain(..idx + 2).collect();
            if let Some(ev) = parse_sse(&raw) {
                on_event(&ev);
                if TERMINAL.contains(&ev.event.as_str()) {
                    return Ok(ev);
                }
            }
        }
    }
    Err(anyhow!("event stream ended before a terminal event"))
}

fn parse_sse(raw: &str) -> Option<SseEvent> {
    let mut event = String::new();
    let mut data = String::new();
    for line in raw.lines() {
        if let Some(rest) = line.strip_prefix("event:") {
            event = rest.trim().to_string();
        } else if let Some(rest) = line.strip_prefix("data:") {
            data = rest.trim().to_string();
        }
    }
    if event.is_empty() {
        return None;
    }
    let data = serde_json::from_str(&data).unwrap_or(Value::Null);
    Some(SseEvent { event, data })
}

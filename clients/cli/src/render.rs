//! Render a Canonical IR to md / vtt / json — the Rust side of ADR-0018's
//! "render both sides". A port of the backend's `render.py`, kept output-compatible:
//! a speaker's label is its `display_name` if named, else its id (S1, S2).

use std::collections::HashMap;

use anyhow::{bail, Result};
use serde_json::Value;

use crate::ir::CanonicalIr;

pub fn render(value: &Value, fmt: &str) -> Result<String> {
    match fmt {
        // Pass the raw IR through so `json` is byte-for-byte the source of truth,
        // not a re-serialization of the typed view.
        "json" => Ok(serde_json::to_string_pretty(value)? + "\n"),
        "md" | "markdown" => Ok(to_markdown(&deserialize(value)?)),
        "vtt" => Ok(to_vtt(&deserialize(value)?)),
        other => bail!("unknown format: {other:?} (use json|md|vtt)"),
    }
}

fn deserialize(value: &Value) -> Result<CanonicalIr> {
    Ok(serde_json::from_value(value.clone())?)
}

fn label_map(ir: &CanonicalIr) -> HashMap<String, String> {
    ir.speakers
        .iter()
        .map(|s| {
            let id = s.id.to_string();
            let label = s.display_name.clone().unwrap_or_else(|| id.clone());
            (id, label)
        })
        .collect()
}

fn ts(seconds: f64) -> String {
    let s = seconds.max(0.0);
    let whole = s as u64;
    let h = whole / 3600;
    let m = (whole % 3600) / 60;
    let sec = whole % 60;
    let ms = ((s - whole as f64) * 1000.0).round() as u64;
    format!("{h:02}:{m:02}:{sec:02}.{ms:03}")
}

struct Block {
    speaker_id: String,
    start: f64,
    text: String,
}

fn group_consecutive(ir: &CanonicalIr) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    for t in &ir.turns {
        let sid = t.speaker_id.to_string();
        match blocks.last_mut() {
            Some(b) if b.speaker_id == sid => {
                b.text.push(' ');
                b.text.push_str(t.text.trim());
            }
            _ => blocks.push(Block {
                speaker_id: sid,
                start: t.start,
                text: t.text.trim().to_string(),
            }),
        }
    }
    blocks
}

fn to_markdown(ir: &CanonicalIr) -> String {
    let labels = label_map(ir);
    let mut lines = vec!["# Transcript".to_string(), String::new()];
    if let Some(uri) = &ir.source.uri {
        lines.push(format!("- **Source:** {uri}"));
    }
    lines.push(format!(
        "- **Duration:** {:.1} min",
        ir.source.duration_s / 60.0
    ));

    let asr = ir.backend.asr.as_deref().unwrap_or("?");
    let backend = match &ir.backend.diarizer {
        Some(d) => format!("- **Backend:** {asr} + {d}"),
        None => format!("- **Backend:** {asr}"),
    };
    lines.push(backend);

    let speakers = ir
        .speakers
        .iter()
        .map(|s| {
            let label = labels
                .get(s.id.as_str())
                .cloned()
                .unwrap_or_else(|| s.id.to_string());
            match &s.role {
                Some(role) => format!("{label} ({role})"),
                None => label,
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    lines.push(format!("- **Speakers:** {speakers}"));
    lines.push(String::new());

    for b in group_consecutive(ir) {
        let label = labels.get(&b.speaker_id).cloned().unwrap_or(b.speaker_id);
        lines.push(format!("**{label}** [{}]", ts(b.start)));
        lines.push(b.text);
        lines.push(String::new());
    }
    lines.join("\n").trim_end().to_string() + "\n"
}

fn to_vtt(ir: &CanonicalIr) -> String {
    let labels = label_map(ir);
    let mut lines = vec!["WEBVTT".to_string(), String::new()];
    for t in &ir.turns {
        let sid = t.speaker_id.to_string();
        let name = labels.get(&sid).cloned().unwrap_or(sid);
        lines.push(format!("{} --> {}", ts(t.start), ts(t.end)));
        lines.push(format!("<v {name}>{}", t.text.trim()));
        lines.push(String::new());
    }
    lines.join("\n").trim_end().to_string() + "\n"
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample() -> Value {
        json!({
            "schema_version": "0.1",
            "source": {"kind": "batch", "uri": "file:///x.wav", "duration_s": 90.0},
            "backend": {"kind": "modular", "asr": "whisper.cpp/large-v3", "diarizer": "pyannote/community-1"},
            "speakers": [
                {"id": "S1", "source": "llm", "display_name": "Aaron", "role": "host"},
                {"id": "S2", "source": "fallback"}
            ],
            "turns": [
                {"speaker_id": "S1", "start": 0.4, "end": 6.8, "text": "Hi there."},
                {"speaker_id": "S1", "start": 6.9, "end": 9.0, "text": "Welcome."},
                {"speaker_id": "S2", "start": 9.1, "end": 12.0, "text": "Thanks."}
            ]
        })
    }

    #[test]
    fn ts_formats_hms_ms() {
        assert_eq!(ts(0.4), "00:00:00.400");
        assert_eq!(ts(3661.5), "01:01:01.500");
        assert_eq!(ts(-5.0), "00:00:00.000");
    }

    #[test]
    fn markdown_labels_groups_and_falls_back() {
        let md = render(&sample(), "md").unwrap();
        assert!(md.contains("- **Speakers:** Aaron (host), S2")); // named, else id
        assert!(md.contains("- **Backend:** whisper.cpp/large-v3 + pyannote/community-1"));
        // consecutive S1 turns merge into one block; S2 starts a new one
        assert!(md.contains("**Aaron** [00:00:00.400]\nHi there. Welcome."));
        assert!(md.contains("**S2** [00:00:09.100]\nThanks."));
    }

    #[test]
    fn vtt_has_voice_cues() {
        let vtt = render(&sample(), "vtt").unwrap();
        assert!(vtt.starts_with("WEBVTT\n"));
        assert!(vtt.contains("00:00:00.400 --> 00:00:06.800\n<v Aaron>Hi there."));
    }

    #[test]
    fn json_is_passthrough_preserving_key_order() {
        let out = render(&sample(), "json").unwrap();
        // schema_version is first in the source, so it must be first out (preserve_order)
        assert!(out.starts_with("{\n  \"schema_version\": \"0.1\""));
        assert!(out.ends_with("}\n"));
    }

    #[test]
    fn unknown_format_errors() {
        assert!(render(&sample(), "pdf").is_err());
    }
}

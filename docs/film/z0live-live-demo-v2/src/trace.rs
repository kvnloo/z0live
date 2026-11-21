use super::{ACCENT, Demo, FAST, HERMES, INK, LIFT, MEDIUM, MONO, MUTE, PIT, Rgb, SANS, SERIF, SLOW, VOICE, YOU, chip, dim, model_name, rgb, smooth, text};
use fframes::Svgr;

struct Span {
    start: f32,
    end: f32,
    answer: Option<String>,
}

struct Track {
    name: &'static str,
    caption: &'static str,
    color: Rgb,
    spans: Vec<Span>,
}

pub(super) struct Trace {
    handoffs: Vec<f32>,
    tracks: [Track; 6],
}

impl Trace {
    pub(super) fn new(demo: &Demo) -> Self {
        let mut tracks = [
            ("CALLER", "TTS · audio activity", YOU),
            ("VOICE", "GPT-LIVE · audio activity", VOICE),
            ("FAST", "Lane", FAST),
            ("MEDIUM", "Lane", MEDIUM),
            ("SLOW", "Lane", SLOW),
            ("HERMES", "12 s stand-in", HERMES),
        ].map(|(name, caption, color)| Track { name, caption, color, spans: Vec::new() });
        tracks[0].spans = speech_spans(&demo.cal);
        tracks[1].spans = speech_spans(&demo.ph);

        let handoffs: Vec<_> = demo.events.iter().filter(|e| e.kind == "handoff").map(|e| e.t).collect();
        for event in &demo.events {
            let (row, elapsed, answer) = match event.kind.as_str() {
                "handoff" => continue,
                "lane" => {
                    let row = match event.tier.as_deref() {
                        Some("fast") => 2, Some("medium") => 3, Some("slow") => 4,
                        _ => panic!("unknown lane tier"),
                    };
                    let elapsed = event.after.expect("lane elapsed time");
                    let label = model_name(event.model.as_deref().expect("lane model"));
                    (row, elapsed, format!("{label} · {elapsed:.2} s"))
                }
                "hermes" => (5, event.s.expect("Hermes stand-in duration"), "Stand-in returned".into()),
                _ => panic!("unknown trace event"),
            };
            let mut starts = handoffs.iter().copied().filter(|&start| {
                start < event.t && (event.t - start - elapsed).abs() <= 0.1
            });
            let start = starts.next().expect("answer without matching hand-off");
            assert!(starts.next().is_none(), "ambiguous hand-off");
            tracks[row].spans.push(Span { start, end: event.t, answer: Some(answer) });
        }
        Self { handoffs, tracks }
    }

    pub(super) fn draw(&self, t: f32, duration: f32) -> Svgr<'static> {
        const X0: f32 = 811.0;
        const X1: f32 = 1798.0;
        const TOP: f32 = 322.0;
        const BOTTOM: f32 = 710.0;
        let x = |seconds| X0 + (X1 - X0) * seconds / duration;
        let head = x(t);
        let mut g = vec![fframes::svgr!(<g>
            <rect x="510" y="144" width="1330" height="604" rx="20" fill={PIT.to_owned()} stroke={LIFT.to_owned()} stroke-width="2"/>
            <path d="M 546 191 V 177 H 560 M 1790 718 H 1804 V 704" fill="none" stroke="#66716a" stroke-width="2"/>
            <rect x={X0} y={TOP} width={head - X0} height={BOTTOM - TOP} fill="#ffffff" opacity="0.025"/>
            {text(570.0, 209.0, 52.0, "Call trace", INK, SERIF, "start", 1.0)}
            {text(552.0, 257.0, 22.0, "Speech activity above. Hand-off to answer below.", MUTE, SANS, "start", 1.0)}
            {text(1798.0, 257.0, 19.0, "DASH: HAND-OFF   ·   DOT: ANSWER", MUTE, MONO, "end", 1.0)}
            {text(552.0, 305.0, 17.0, "RECORDING TIME", ACCENT, MONO, "start", 1.0)}
        </g>)];
        for sec in (0..duration.ceil() as u32).step_by(5) {
            let xx = x(sec as f32);
            g.push(fframes::svgr!(<line x1={xx} x2={xx} y1={TOP} y2={BOTTOM} stroke="#313a38" stroke-width="1"/>));
            g.push(text(xx, 305.0, 18.0, &format!("{sec}s"), MUTE, MONO, if sec == 0 { "start" } else { "middle" }, 1.0));
        }
        for &start in self.handoffs.iter().filter(|&&start| start <= t) {
            let xx = x(start);
            g.push(fframes::svgr!(<line x1={xx} x2={xx} y1={TOP} y2={BOTTOM} stroke={rgb(VOICE)} stroke-width="2" stroke-dasharray="4 6" opacity="0.65"/>));
            let age = t - start;
            g.push(chip(1437.0, 201.0, "HAND-OFF → lanes + Hermes", VOICE, smooth(age / 0.5) * smooth((3.0 - age) / 0.6)));
        }
        for (i, track) in self.tracks.iter().enumerate() {
            let y = TOP + 35.0 + i as f32 * 61.0 + if i >= 2 { 11.0 } else { 0.0 };
            let c = rgb(track.color);
            let muted = rgb(dim(track.color, 0.5));
            let latest = track.spans.iter().rev().find(|s| s.end <= t).and_then(|s| s.answer.as_deref());
            let caption = if i == 5 { track.caption } else { latest.unwrap_or(track.caption) };
            g.push(fframes::svgr!(<g>
                <line x1={X0} x2={X1} y1={y} y2={y} stroke="#3a4440" stroke-width="1"/>
                {text(552.0, y - 4.0, 22.0, track.name, &c, MONO, "start", 1.0)}
                {text(552.0, y + 20.0, 18.0, caption, MUTE, SANS, "start", 1.0)}
            </g>));
            for (j, span) in track.spans.iter().enumerate().filter(|(_, span)| span.start < t) {
                // Hermes takes can overlap. Keep both starts and answer dots visible in one row.
                let y = if i == 5 { y + if j % 2 == 0 { -9.0 } else { 9.0 } } else { y };
                let xx = x(span.start);
                let width = x(t.min(span.end)) - xx;
                let fill = if t < span.end { &c } else { &muted };
                let stroke = if i == 5 { "#f8b29c" } else { c.as_str() };
                g.push(fframes::svgr!(<rect x={xx} y={y - 8.0} width={width} height="16" rx="3" fill={fill.clone()} stroke={stroke.to_owned()} stroke-width="1"/>));
                if t >= span.end && span.answer.is_some() {
                    g.push(fframes::svgr!(<circle cx={x(span.end)} cy={y} r="6" fill={c.clone()} stroke={PIT.to_owned()} stroke-width="2"/>));
                }
            }
        }
        g.push(fframes::svgr!(<g><line x1={head} x2={head} y1={TOP} y2={BOTTOM} stroke={INK.to_owned()} stroke-width="2" opacity="0.75"/>
            <circle cx={head} cy={TOP} r="4" fill={INK.to_owned()}/></g>));
        fframes::svgr!(<g>{g}</g>)
    }
}

fn speech_spans(envelope: &[f32]) -> Vec<Span> {
    // Group RMS activity across pauses of up to 250 ms. Reject bursts under 100 ms.
    let mut spans = Vec::new();
    let mut first = None;
    let mut last = 0;
    for (frame, _) in envelope.iter().enumerate().filter(|(_, level)| **level >= 0.08) {
        if let Some(start) = first {
            if frame - last > 16 {
                if last + 1 - start >= 6 {
                    spans.push(Span { start: start as f32 / 60.0, end: (last + 1) as f32 / 60.0, answer: None });
                }
                first = None;
            }
        }
        first.get_or_insert(frame);
        last = frame;
    }
    if let Some(start) = first.filter(|&start| last + 1 - start >= 6) {
        spans.push(Span { start: start as f32 / 60.0, end: (last + 1) as f32 / 60.0, answer: None });
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::DEMO;

    #[test]
    fn overlapping_rounds_keep_their_own_answers() {
        let trace = Trace::new(&DEMO);
        let endpoints = |row: usize| trace.tracks[row].spans.iter().map(|s| (s.start, s.end)).collect::<Vec<_>>();
        assert_eq!(endpoints(2), [(20.08, 21.45), (31.31, 33.12)]);
        assert_eq!(endpoints(3), [(20.08, 22.11), (31.31, 33.40)]);
        assert_eq!(endpoints(4), [(20.08, 22.27), (31.31, 33.82)]);
        assert_eq!(endpoints(5), [(20.08, 32.08), (31.31, 43.31)]);
    }

    #[test]
    fn speech_spans_use_sample_edges_without_fabricating_answers() {
        let mut envelope = [0.0; 61];
        envelope[4..10].fill(0.5);
        envelope[24..30].fill(0.5);
        envelope[58..].fill(0.5);
        let spans = speech_spans(&envelope);
        assert_eq!(spans.len(), 1);
        assert_eq!((spans[0].start, spans[0].end), (4.0 / 60.0, 30.0 / 60.0));
        assert!(spans[0].answer.is_none());
    }
}

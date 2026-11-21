//! z0live live demo v1 (fframes 1.3, 1920x1080, 60 fps): a REAL z0live call (take 2, 2026-11-20), rendered from its
//! own data. demo.json holds the caller and phone audio envelopes (60 fps RMS), the transcript lines at their audio
//! times, and the trace events (hand-off, each lane answer, Hermes) shifted onto the audio clock (trace t - 0.70 s,
//! +-0.1 s). The soundtrack (mix.wav) is muxed after render. Labelled on screen: the caller voice is text-to-speech and
//! the Hermes answer is a 12 s stand-in. Orbs: the real thinking-orbs engine, baked per frame (glide-hackathon-v3).
use fframes::{
    AudioMap, Color, CombinedMediaProvider, Duration, EncoderOptions, FFramesContext, Frame, MediaDirectory, MediaProvider,
    RenderOptions, Scenes, Svgr, Video, cli,
};
use fframes_skia_renderer::cli::SkiaRenderers;
use serde::Deserialize;
use std::path::Path;
use std::sync::LazyLock;

fframes::include_media_dir!(pub struct Media, "media");

const SANS: &str = "Inter";
const MONO: &str = "IBM Plex Mono";
const SERIF: &str = "Instrument Serif";
const GROUND: &str = "#181f23";
const PIT: &str = "#12171a";
const LIFT: &str = "#252c30";
const INK: &str = "#d1deda";
const MUTE: &str = "#929b98";
const ACCENT: &str = "#c8be72";
type Rgb = (u8, u8, u8);
const YOU: Rgb = (152, 179, 158);
const VOICE: Rgb = (200, 190, 114);
const FAST: Rgb = (79, 165, 179);
const MEDIUM: Rgb = (153, 163, 98);
const SLOW: Rgb = (103, 140, 62);
const HERMES: Rgb = (214, 128, 100);

#[derive(Deserialize)]
struct Line { who: String, t: f32, text: String }
#[derive(Deserialize)]
struct Ev { kind: String, t: f32, tier: Option<String>, model: Option<String>, after: Option<f32> }
#[derive(Deserialize)]
struct Demo { duration: f32, cal: Vec<f32>, ph: Vec<f32>, lines: Vec<Line>, events: Vec<Ev> }
static DEMO: LazyLock<Demo> = LazyLock::new(|| {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("demo.json");
    serde_json::from_str(&std::fs::read_to_string(p).expect("demo.json")).expect("demo parse")
});

fn clamp01(x: f32) -> f32 { x.clamp(0.0, 1.0) }
fn prog(t: f32, a: f32, b: f32) -> f32 { clamp01((t - a) / (b - a)) }
fn lerp(a: f32, b: f32, k: f32) -> f32 { a + (b - a) * k }
fn expo_out(x: f32) -> f32 { if x >= 1.0 { 1.0 } else { 1.0 - 2f32.powf(-10.0 * clamp01(x)) } }
fn rgb(c: Rgb) -> String { format!("#{:02x}{:02x}{:02x}", c.0, c.1, c.2) }
fn dim(c: Rgb, k: f32) -> Rgb { let m = |x: u8, y: u8| lerp(x as f32, y as f32, k).round() as u8; (m(c.0, 0x29), m(c.1, 0x30), m(c.2, 0x34)) }
/// Envelope value at t, lightly smoothed over 3 frames.
fn level(v: &[f32], t: f32) -> f32 {
    let i = (t * 60.0) as isize;
    let g = |j: isize| if j >= 0 && (j as usize) < v.len() { v[j as usize] } else { 0.0 };
    (g(i - 1) + 2.0 * g(i) + g(i + 1)) / 4.0
}

fn text(x: f32, y: f32, size: f32, s: &str, fill: &str, font: &str, anchor: &str, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let ls = if font == MONO { "0.04em" } else { "-0.02em" };
    fframes::svgr!(<text x={x} y={y} font-size={size} fill={fill.to_owned()} font-family={font.to_owned()} text-anchor={anchor.to_owned()} letter-spacing={ls} opacity={o}>{s.to_owned()}</text>)
}

// ---------- the real thinking-orbs orb (glide-hackathon-v3)
#[derive(Deserialize)]
struct Orb { size: f32, frames: Vec<Vec<f32>>, lines: Vec<Vec<f32>> }
const ORB_STATES: [&str; 9] = ["breathing", "working", "listening", "searching", "connecting", "solving", "weaving", "composing", "shaping"];
static ORBS: LazyLock<Vec<Orb>> = LazyLock::new(|| ORB_STATES.iter().map(|s| {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("orbs").join(format!("{s}.json"));
    serde_json::from_str(&std::fs::read_to_string(&p).expect("orb json")).expect("orb parse")
}).collect());
fn mix_hex(a: Rgb, w: f32) -> String { let m = |c: u8| (c as f32 + (255.0 - c as f32) * clamp01(w)).round() as u8; format!("#{:02x}{:02x}{:02x}", m(a.0), m(a.1), m(a.2)) }
fn orb(t: f32, which: u8, cx: f32, cy: f32, d: f32, base: Rgb, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let orb = &ORBS[which as usize % ORBS.len()];
    let i = ((t.max(0.0) * 60.0) as usize) % orb.frames.len();
    let s = d / orb.size;
    let mut g = Vec::new();
    for q in orb.frames[i].chunks_exact(5) {
        if q[4] <= 0.01 { continue; }
        g.push(fframes::svgr!(<circle cx={cx + (q[0] - orb.size / 2.0) * s} cy={cy + (q[1] - orb.size / 2.0) * s} r={(q[2] * s).max(0.6)} fill={mix_hex(base, q[3])} opacity={q[4] * o}/>));
    }
    for q in orb.lines[i].chunks_exact(7) {
        g.push(fframes::svgr!(<line x1={cx + (q[0] - orb.size / 2.0) * s} y1={cy + (q[1] - orb.size / 2.0) * s} x2={cx + (q[2] - orb.size / 2.0) * s} y2={cy + (q[3] - orb.size / 2.0) * s} stroke={mix_hex(base, q[5])} stroke-width={q[4] * s} opacity={q[6] * o}/>));
    }
    fframes::svgr!(<g>{g}</g>)
}
/// A speaker orb driven by its real audio level.
fn speaker(t: f32, cx: f32, cy: f32, d: f32, c: Rgb, lv: f32, label: &str, sub: &str) -> Svgr<'static> {
    let on = clamp01((lv - 0.08) * 3.0);
    let ring = if on > 0.01 {
        fframes::svgr!(<circle cx={cx} cy={cy} r={d * 0.5 + 12.0 + 40.0 * lv} fill="none" stroke={rgb(c)} stroke-width="3" opacity={0.45 * on}/>)
    } else { Svgr::empty() };
    fframes::svgr!(<g>{ring}{orb(t, if on > 0.05 { 7 } else { 2 }, cx, cy, d * (1.0 + 0.10 * lv), if on > 0.05 { c } else { dim(c, 0.35) }, 1.0)}
        {text(cx, cy + d * 0.5 + 60.0, 26.0, label, &rgb(c), MONO, "middle", 1.0)}
        {text(cx, cy + d * 0.5 + 92.0, 20.0, sub, MUTE, SANS, "middle", 1.0)}</g>)
}

fn chip(x: f32, y: f32, s: &str, c: Rgb, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let w = 24.0 + s.chars().count() as f32 * 12.6;
    fframes::svgr!(<g opacity={o}><rect x={x} y={y - 25.0} width={w} height="34" rx="17" fill={rgb(dim(c, 0.72))} stroke={rgb(c)} stroke-width="1.5"/>
        {text(x + w / 2.0, y - 2.0, 19.0, s, &rgb(c), MONO, "middle", 1.0)}</g>)
}

fn model_name(m: &str) -> String {
    match m {
        "gpt-6-luna" => "GPT-6 Luna".into(), "gpt-6.1-terra" => "GPT-6.1 Terra".into(), "gpt-6.2-sol" => "GPT-6.2 Sol".into(),
        "claude-haiku-5-5" => "Haiku 5.5".into(), "claude-sonnet-5-5" => "Sonnet 5.5".into(), "claude-opus-5-5" => "Opus 5.5".into(),
        o => o.to_string(),
    }
}

fn film(t: f32) -> Svgr<'static> {
    let d = &*DEMO;
    let mut l: Vec<Svgr> = Vec::new();
    l.push(fframes::svgr!(<g>
        <defs><radialGradient id="room" cx="0.3" cy="0.4" r="0.95"><stop offset="0" stop-color={LIFT.to_owned()}/><stop offset="1" stop-color={PIT.to_owned()}/></radialGradient></defs>
        <rect width="1920" height="1080" fill={GROUND.to_owned()}/><rect width="1920" height="1080" fill="url(#room)" opacity="0.85"/>
    </g>));
    // header
    l.push(text(80.0, 96.0, 44.0, "z0live", INK, SERIF, "start", 1.0));
    l.push(text(206.0, 96.0, 22.0, "LIVE CALL · REAL RECORDING", ACCENT, MONO, "start", 1.0));
    l.push(text(1840.0, 96.0, 26.0, &format!("{:5.1} s", t), MUTE, MONO, "end", 1.0));
    // speakers
    l.push(speaker(t, 300.0, 330.0, 220.0, YOU, level(&d.cal, t), "YOU", "caller (text-to-speech)"));
    l.push(speaker(t + 1.7, 300.0, 720.0, 220.0, VOICE, level(&d.ph, t), "VOICE · GPT-LIVE", "speaks to you"));

    // lanes panel
    let (px, py) = (640.0, 170.0);
    l.push(fframes::svgr!(<g><rect x={px} y={py} width="1200" height="470" rx="24" fill={PIT.to_owned()} stroke={LIFT.to_owned()} stroke-width="2"/></g>));
    l.push(text(px + 32.0, py + 50.0, 22.0, "LANES · SHARED CONTEXT", ACCENT, MONO, "start", 1.0));
    l.push(text(px + 1168.0, py + 50.0, 20.0, "first real answer is spoken; later ones only if they add something", MUTE, SANS, "end", 1.0));
    let rows: [(&str, Rgb); 4] = [("fast", FAST), ("medium", MEDIUM), ("slow", SLOW), ("hermes", HERMES)];
    for (i, (name, c)) in rows.iter().enumerate() {
        let y = py + 130.0 + i as f32 * 92.0;
        // the latest event of this row up to now
        let last = d.events.iter().filter(|e| e.t <= t && ((e.kind == "lane" && e.tier.as_deref() == Some(name)) || (e.kind == "hermes" && *name == "hermes"))).last();
        let age = last.map(|e| t - e.t).unwrap_or(99.0);
        let glow = clamp01(1.0 - age / 3.0);
        let busy = d.events.iter().any(|e| e.kind == "handoff" && e.t <= t && t - e.t < 2.6) && glow < 0.05 && *name != "hermes"
            || (*name == "hermes" && d.events.iter().any(|e| e.kind == "handoff" && e.t <= t) && glow < 0.05 && d.events.iter().filter(|e| e.kind == "hermes" && e.t <= t).count() < d.events.iter().filter(|e| e.kind == "handoff" && e.t <= t).count());
        let state = if glow > 0.05 { 7 } else if busy { 3 } else { 0 };
        l.push(orb(t + i as f32 * 0.9, state, px + 80.0, y - 8.0, 70.0, if glow > 0.05 || busy { *c } else { dim(*c, 0.45) }, 1.0));
        l.push(text(px + 140.0, y, 24.0, &name.to_uppercase(), &rgb(*c), MONO, "start", 1.0));
        if let Some(e) = last {
            let label = if e.kind == "hermes" { "Hermes agent · stand-in answer".to_string() } else { model_name(e.model.as_deref().unwrap_or("")) };
            l.push(text(px + 330.0, y, 26.0, &label, INK, SANS, "start", 0.35 + 0.65 * glow));
            if let Some(a) = e.after { l.push(chip(px + 760.0, y, &format!("{:.2} s after hand-off", a), *c, 0.4 + 0.6 * glow)); }
            if e.kind == "hermes" { l.push(chip(px + 760.0, y, "12 s after hand-off", *c, 0.4 + 0.6 * glow)); }
        } else if busy {
            l.push(text(px + 330.0, y, 24.0, "thinking…", MUTE, SANS, "start", 1.0));
        }
    }
    // hand-off flash: a pulse from the voice orb to the panel
    for e in d.events.iter().filter(|e| e.kind == "handoff") {
        let k = prog(t, e.t, e.t + 0.6);
        if k > 0.0 && k < 1.0 {
            let x = lerp(410.0, px, expo_out(k));
            let y = lerp(690.0, py + 220.0, expo_out(k));
            l.push(fframes::svgr!(<circle cx={x} cy={y} r="11" fill={rgb(VOICE)}/>));
        }
        let o = prog(t, e.t, e.t + 0.2) * (1.0 - prog(t, e.t + 1.6, e.t + 2.0));
        l.push(chip(px + 32.0, py + 96.0, "HAND-OFF → lanes + Hermes", VOICE, o));
    }

    // transcript: last 3 lines, typed at ~16 chars/s from their real start time
    let (tx, ty) = (640.0, 700.0);
    l.push(fframes::svgr!(<g><rect x={tx} y={ty} width="1200" height="270" rx="24" fill={PIT.to_owned()} stroke={LIFT.to_owned()} stroke-width="2"/></g>));
    l.push(text(tx + 32.0, ty + 46.0, 20.0, "TRANSCRIPT", ACCENT, MONO, "start", 1.0));
    let shown: Vec<&Line> = d.lines.iter().filter(|x| x.t <= t).collect();
    let recent = &shown[shown.len().saturating_sub(3)..];
    for (j, ln) in recent.iter().enumerate() {
        let n = (((t - ln.t) * 16.0) as usize).min(ln.text.chars().count());
        let mut s: String = ln.text.chars().take(n).collect();
        if s.chars().count() > 78 { s = format!("…{}", s.chars().rev().take(77).collect::<Vec<_>>().into_iter().rev().collect::<String>()); }
        let (who, c) = if ln.who == "you" { ("YOU", YOU) } else { ("VOICE", VOICE) };
        let y = ty + 106.0 + j as f32 * 56.0;
        l.push(text(tx + 32.0, y, 20.0, who, &rgb(c), MONO, "start", 1.0));
        l.push(text(tx + 140.0, y, 26.0, &s, INK, SANS, "start", 1.0));
    }
    l.push(text(1880.0, 1062.0, 16.0, "REAL z0live CALL (STUDIO, GPT-LIVE + TIER LANES) · CALLER VOICE: TTS · HERMES ANSWER: 12 s STAND-IN · EVENTS ALIGNED TO AUDIO ±0.1 s", "#5d6663", MONO, "end", 1.0));
    fframes::svgr!(<g>{l}</g>)
}

struct Film;
impl Video for Film {
    const FPS: usize = 60;
    const WIDTH: usize = 1920;
    const HEIGHT: usize = 1080;
    const BACKGROUND_COLOR: Color = Color::BLACK;
    fn duration(&self) -> Duration<'_> { Duration::Frames((DEMO.duration * 60.0).floor() as usize) }
    fn audio(&self) -> AudioMap<'_> { AudioMap::none() }
    fn define_scenes(&self) -> Scenes<'_> { Scenes::from(Vec::<&dyn fframes::Scene>::new()) }
    fn render_frame<'a>(&'a self, frame: Frame, _ctx: &FFramesContext<'a, '_>) -> Svgr<'a> {
        let t = frame.global_index as f32 / 60.0;
        fframes::svgr!(<svg width="1920" height="1080">{film(t)}</svg>)
    }
}

fn main() -> std::process::ExitCode {
    eprintln!("z0live-live-demo-v1 ({} s)", DEMO.duration);
    let media = Media::new().expect("media");
    let dir = MediaDirectory::read_folder(&Path::new(env!("CARGO_MANIFEST_DIR")).join("dynamic_media")).expect("dynamic_media");
    let dynamic = dir.process_media_source().expect("dynamic media");
    let all = CombinedMediaProvider::from([&media as &dyn MediaProvider, &dynamic]);
    cli::new(&Film, RenderOptions {
        media: Some(&all),
        default_font: SANS,
        video_encoder_options: EncoderOptions {
            preferred_encoder: Some("ffv1"),
            pixel_format: fframes::AVPixelFormat::AV_PIX_FMT_BGR0,
            color_matrix: fframes::YuvMatrix::Bt709,
            ..Default::default()
        },
        ..Default::default()
    })
    .renderers(SkiaRenderers::gpu(Default::default()))
    .default_output("out/master.mkv")
    .run()
}

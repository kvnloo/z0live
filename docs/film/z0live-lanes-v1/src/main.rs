//! z0live lanes film v1 (fframes 1.3, 1920x1080, 60 fps, ~34 s, silent-safe captions).
//! Story: one call, many minds, one context. The voice orb talks; fast/medium/slow lanes and Hermes all read the
//! SAME context window; the first real answer is spoken, later ones only if they add something; a second voice
//! (Grok) takes the floor when called by name and its words join the shared context.
//!
//! The orbs are the real thinking-orbs engine (kvnloo/thinking-orbs), baked per frame by glide-hackathon-v3's
//! tools/bake_orb.mjs and drawn as vector dots, the same way glide-hackathon-v3 draws its voice orb.
//! Timings shown on screen come from real z0live traces (fast lane spoken 1.3-2.1 s; Grok 2.4 s after its name);
//! the example question and answers are scripted.
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
const LEN: f32 = 34.0;

// the desktop host-baseline palette (glide-hackathon-v3/src/theme.json)
const GROUND: &str = "#181f23";
const PIT: &str = "#12171a";
const LIFT: &str = "#252c30";
const INK: &str = "#d1deda";
const MUTE: &str = "#929b98";
const ACCENT: &str = "#c8be72";
type Rgb = (u8, u8, u8);
const VOICE: Rgb = (200, 190, 114);
const FAST: Rgb = (79, 165, 179);
const MEDIUM: Rgb = (153, 163, 98);
const SLOW: Rgb = (103, 140, 62);
const HERMES: Rgb = (152, 179, 158);
const GROK: Rgb = (214, 128, 100);

// ---------- math (glide-hackathon-v3)
fn clamp01(x: f32) -> f32 { x.clamp(0.0, 1.0) }
fn prog(t: f32, a: f32, b: f32) -> f32 { clamp01((t - a) / (b - a)) }
fn lerp(a: f32, b: f32, k: f32) -> f32 { a + (b - a) * k }
fn bez(x: f32, x1: f32, y1: f32, x2: f32, y2: f32) -> f32 {
    let x = clamp01(x);
    let f = |s: f32, a: f32, b: f32| { let u = 1.0 - s; 3.0 * u * u * s * a + 3.0 * u * s * s * b + s * s * s };
    let (mut lo, mut hi) = (0.0f32, 1.0f32);
    for _ in 0..24 { let m = (lo + hi) * 0.5; if f(m, x1, x2) < x { lo = m } else { hi = m } }
    f((lo + hi) * 0.5, y1, y2)
}
fn io(x: f32) -> f32 { bez(x, 0.45, 0.0, 0.1, 1.0) }
fn expo_out(x: f32) -> f32 { if x >= 1.0 { 1.0 } else { 1.0 - 2f32.powf(-10.0 * clamp01(x)) } }
fn back_out(x: f32) -> f32 { let x = clamp01(x); let c = 1.70158; 1.0 + (c + 1.0) * (x - 1.0).powi(3) + c * (x - 1.0).powi(2) }
/// Fade in over [a, a+f], out over [b-f, b].
fn window(t: f32, a: f32, b: f32, f: f32) -> f32 { prog(t, a, a + f) * (1.0 - prog(t, b - f, b)) }
fn rgb(c: Rgb) -> String { format!("#{:02x}{:02x}{:02x}", c.0, c.1, c.2) }
fn dim(c: Rgb, k: f32) -> Rgb {
    let m = |x: u8, y: u8| lerp(x as f32, y as f32, k).round() as u8;
    (m(c.0, 0x29), m(c.1, 0x30), m(c.2, 0x34))
}

// ---------- type
fn text(x: f32, y: f32, size: f32, s: &str, fill: &str, font: &str, anchor: &str, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let ls = if font == MONO { "0.04em" } else { "-0.02em" };
    fframes::svgr!(<text x={x} y={y} font-size={size} fill={fill.to_owned()} font-family={font.to_owned()} text-anchor={anchor.to_owned()} letter-spacing={ls} opacity={o}>{s.to_owned()}</text>)
}
fn rise(t: f32, at: f32, x: f32, y: f32, size: f32, s: &str, fill: &str, font: &str, anchor: &str) -> Svgr<'static> {
    let k = expo_out(prog(t, at, at + 0.45));
    if k <= 0.0 { return Svgr::empty(); }
    fframes::svgr!(<g transform={format!("translate(0 {})", (1.0 - k) * size * 0.35)}>{text(x, y, size, s, fill, font, anchor, k)}</g>)
}
fn typed(t: f32, at: f32, cps: f32, s: &str) -> String { let n = ((t - at) * cps).max(0.0) as usize; s.chars().take(n).collect() }

// ---------- the real thinking-orbs orb, baked per frame (glide-hackathon-v3)
#[derive(Deserialize)]
struct Orb { size: f32, frames: Vec<Vec<f32>>, lines: Vec<Vec<f32>> }
fn load_orb(state: &str) -> Orb {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("orbs").join(format!("{state}.json"));
    serde_json::from_str(&std::fs::read_to_string(&p).expect("orb json")).expect("orb parse")
}
const ORB_STATES: [&str; 9] = ["breathing", "working", "listening", "searching", "connecting", "solving", "weaving", "composing", "shaping"];
const BREATHING: u8 = 0;
const WORKING: u8 = 1;
const LISTENING: u8 = 2;
const SEARCHING: u8 = 3;
const SOLVING: u8 = 5;
const COMPOSING: u8 = 7;
static ORBS: LazyLock<Vec<Orb>> = LazyLock::new(|| ORB_STATES.iter().map(|s| load_orb(s)).collect());
fn mix_hex(a: Rgb, w: f32) -> String {
    let m = |c: u8| (c as f32 + (255.0 - c as f32) * clamp01(w)).round() as u8;
    format!("#{:02x}{:02x}{:02x}", m(a.0), m(a.1), m(a.2))
}
fn orb(t: f32, which: u8, cx: f32, cy: f32, d: f32, base: Rgb, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let orb = &ORBS[which as usize % ORBS.len()];
    let i = ((t.max(0.0) * 60.0) as usize) % orb.frames.len();
    let s = d / orb.size;
    let mut dots = Vec::with_capacity(orb.frames[i].len() / 5);
    for q in orb.frames[i].chunks_exact(5) {
        let (x, y, r, w, a) = (q[0], q[1], q[2], q[3], q[4]);
        if a <= 0.01 { continue; }
        dots.push(fframes::svgr!(<circle cx={cx + (x - orb.size / 2.0) * s} cy={cy + (y - orb.size / 2.0) * s} r={(r * s).max(0.6)} fill={mix_hex(base, w)} opacity={a * o}/>));
    }
    for q in orb.lines[i].chunks_exact(7) {
        dots.push(fframes::svgr!(<line x1={cx + (q[0] - orb.size / 2.0) * s} y1={cy + (q[1] - orb.size / 2.0) * s} x2={cx + (q[2] - orb.size / 2.0) * s} y2={cy + (q[3] - orb.size / 2.0) * s} stroke={mix_hex(base, q[5])} stroke-width={q[4] * s} opacity={q[6] * o}/>));
    }
    fframes::svgr!(<g>{dots}</g>)
}
/// A talking orb: the composing orb whose size follows a speech envelope (syllable-rate wobble while `on`).
fn talking(t: f32, cx: f32, cy: f32, d: f32, base: Rgb, on: f32, o: f32) -> Svgr<'static> {
    let env = on * (0.5 + 0.5 * (t * 23.0).sin() * (t * 7.3).sin()).abs();
    let ring = if on > 0.01 {
        let r = d * 0.5 + 14.0 + 26.0 * env;
        fframes::svgr!(<circle cx={cx} cy={cy} r={r} fill="none" stroke={rgb(base)} stroke-width="2.5" opacity={0.35 * on * o}/>)
    } else { Svgr::empty() };
    fframes::svgr!(<g>{ring}{orb(t, if on > 0.01 { COMPOSING } else { LISTENING }, cx, cy, d * (1.0 + 0.06 * env), base, o)}</g>)
}

// ---------- layout
const PANEL: (f32, f32, f32, f32) = (760.0, 610.0, 900.0, 330.0); // the shared context window
const ROWS: [f32; 3] = [688.0, 788.0, 888.0];
const VAL: f32 = 300.0; // x offset of row values inside the panel
struct Lane { name: &'static str, models: &'static str, x: f32, d: f32, c: Rgb }
const LANES: [Lane; 4] = [
    Lane { name: "FAST", models: "Haiku 5.5 · GPT-6 Luna · Gemma 4 E4B", x: 860.0, d: 120.0, c: FAST },
    Lane { name: "MEDIUM", models: "Sonnet 5.5 · GPT-6.1 Terra", x: 1100.0, d: 120.0, c: MEDIUM },
    Lane { name: "SLOW", models: "Opus 5.5 · GPT-6.2 Sol", x: 1340.0, d: 120.0, c: SLOW },
    Lane { name: "HERMES", models: "the agent · full tools", x: 1580.0, d: 150.0, c: HERMES },
];
const LANE_Y: f32 = 330.0;

fn seg(x1: f32, y1: f32, x2: f32, y2: f32, c: &str, w: f32, o: f32, dash: bool) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let da = if dash { "6 10" } else { "none" };
    fframes::svgr!(<line x1={x1} y1={y1} x2={x2} y2={y2} stroke={c.to_owned()} stroke-width={w} stroke-dasharray={da} stroke-linecap="round" opacity={o}/>)
}
/// A bright dot travelling from (x1,y1) to (x2,y2) over [a,b], with a short trail.
fn pulse(t: f32, a: f32, b: f32, x1: f32, y1: f32, x2: f32, y2: f32, c: Rgb) -> Svgr<'static> {
    let k = prog(t, a, b);
    if k <= 0.0 || k >= 1.0 { return Svgr::empty(); }
    let mut g = Vec::new();
    for j in 0..6 {
        let kk = io((k - j as f32 * 0.035).max(0.0));
        g.push(fframes::svgr!(<circle cx={lerp(x1, x2, kk)} cy={lerp(y1, y2, kk)} r={9.0 - j as f32 * 1.2} fill={rgb(c)} opacity={1.0 - j as f32 * 0.16}/>));
    }
    fframes::svgr!(<g>{g}</g>)
}
fn chip(x: f32, y: f32, s: &str, c: Rgb, o: f32) -> Svgr<'static> {
    if o <= 0.0 { return Svgr::empty(); }
    let w = 22.0 + s.chars().count() as f32 * 13.2;
    fframes::svgr!(<g opacity={o}>
        <rect x={x} y={y - 26.0} width={w} height="36" rx="18" fill={rgb(dim(c, 0.72))} stroke={rgb(c)} stroke-width="1.5"/>
        {text(x + w / 2.0, y - 2.0, 20.0, s, &rgb(c), MONO, "middle", 1.0)}
    </g>)
}

// ---------- the film
fn film(t: f32) -> Svgr<'static> {
    let mut l: Vec<Svgr> = Vec::new();
    l.push(fframes::svgr!(<g>
        <defs><radialGradient id="room" cx="0.38" cy="0.42" r="0.9"><stop offset="0" stop-color={LIFT.to_owned()}/><stop offset="1" stop-color={PIT.to_owned()}/></radialGradient></defs>
        <rect width="1920" height="1080" fill={GROUND.to_owned()}/>
        <rect width="1920" height="1080" fill="url(#room)" opacity="0.85"/>
    </g>));

    // Voice orb (GPT-Live): centre stage, then to the left; in the two-voice beat it slides up for Grok.
    let to_left = io(prog(t, 4.6, 5.8));
    let duet = io(prog(t, 22.0, 22.8)) * (1.0 - io(prog(t, 29.0, 29.8)));
    let vx = lerp(960.0, 400.0, to_left);
    let vy = lerp(lerp(440.0, 520.0, to_left), 400.0, duet);
    let vd = lerp(lerp(380.0, 300.0, to_left), 230.0, duet);
    let grok_floor = window(t, 25.2, 28.0, 0.25);
    let voice_on = window(t, 14.35, 17.6, 0.2) + window(t, 20.3, 22.0, 0.2) + window(t, 28.4, 29.2, 0.2);
    let end = io(prog(t, 29.4, 30.4));
    let voice_o = 1.0 - 0.55 * grok_floor;
    l.push(talking(t, vx, vy, vd * (1.0 - 0.25 * end), dim(VOICE, 0.55 * grok_floor), clamp01(voice_on), voice_o * (1.0 - end)));
    l.push(text(vx, vy + vd * 0.5 + 54.0, 22.0, "VOICE · GPT-LIVE", &rgb(VOICE), MONO, "middle", window(t, 5.6, 29.4, 0.4) * voice_o));

    // 1. Payoff: one call, many minds.
    if t < 4.8 {
        let o = 1.0 - prog(t, 4.3, 4.8);
        l.push(fframes::svgr!(<g opacity={o}>
            {rise(t, 0.3, 960.0, 790.0, 96.0, "One call. Many minds.", INK, SERIF, "middle")}
            {rise(t, 0.9, 960.0, 860.0, 30.0, "z0live · voice lanes for Hermes", MUTE, MONO, "middle")}
        </g>));
    }

    // 2. The shared context window and the lanes that all read it.
    let panel_o = window(t, 5.4, 30.0, 0.5);
    if panel_o > 0.0 {
        let k = back_out(prog(t, 5.4, 6.1));
        let (px, py, pw, ph) = PANEL;
        l.push(fframes::svgr!(<g opacity={panel_o} transform={format!("translate({} {}) scale({}) translate({} {})", px + pw / 2.0, py + ph / 2.0, 0.92 + 0.08 * k, -(px + pw / 2.0), -(py + ph / 2.0))}>
            <rect x={px + 8.0} y={py + 18.0} width={pw} height={ph} rx="26" fill="#000000" opacity="0.35"/>
            <rect x={px} y={py} width={pw} height={ph} rx="26" fill={PIT.to_owned()} stroke={ACCENT.to_owned()} stroke-width="2"/>
            {text(px + 34.0, py + 46.0, 22.0, "SHARED CONTEXT WINDOW", ACCENT, MONO, "start", 1.0)}
            {text(px + pw - 34.0, py + 46.0, 22.0, "one copy · every voice and lane", MUTE, MONO, "end", 1.0)}
        </g>));
        let rows: [(&str, f32); 3] = [("chat brief", 6.0), ("live transcript", 6.25), ("blackboard", 6.5)];
        for (i, (label, at)) in rows.iter().enumerate() {
            let o = prog(t, *at, at + 0.35) * panel_o;
            l.push(text(PANEL.0 + 34.0, ROWS[i] + 8.0, 22.0, label, MUTE, MONO, "start", o));
            l.push(seg(PANEL.0 + VAL, ROWS[i] + 28.0, PANEL.0 + PANEL.2 - 34.0, ROWS[i] + 28.0, LIFT, 1.5, o, false));
        }
        l.push(text(PANEL.0 + VAL, ROWS[0] + 8.0, 26.0, "Kevin's chat: last 12 turns, open tasks, today", INK, SANS, "start", prog(t, 6.2, 6.6) * panel_o));
    }
    for (i, lane) in LANES.iter().enumerate() {
        let at = 6.6 + i as f32 * 0.28;
        let o = expo_out(prog(t, at, at + 0.5)) * window(t, at, 30.0, 0.5);
        if o <= 0.0 { continue; }
        // every lane is wired to the same window
        l.push(seg(lane.x, LANE_Y + lane.d * 0.5 + 12.0, lane.x.clamp(PANEL.0 + 40.0, PANEL.0 + PANEL.2 - 40.0), PANEL.1, &rgb(lane.c), 2.0, 0.55 * o, true));
        let state = lane_state(t, i);
        l.push(orb(t + i as f32 * 0.7, state, lane.x, LANE_Y, lane.d, lane.c, o));
        let models: Vec<&str> = lane.models.split(" · ").collect();
        let base = LANE_Y - lane.d * 0.5 - 22.0;
        for (j, m) in models.iter().enumerate() {
            l.push(text(lane.x, base - (models.len() - 1 - j) as f32 * 27.0, 21.0, m, MUTE, SANS, "middle", o));
        }
        l.push(text(lane.x, base - models.len() as f32 * 27.0 - 10.0, 24.0, lane.name, &rgb(lane.c), MONO, "middle", o));
    }
    // voice orb reads the window too
    l.push(seg(vx + 160.0, vy + 40.0, PANEL.0, PANEL.1 + 120.0, &rgb(VOICE), 2.0, 0.5 * window(t, 7.6, 29.4, 0.5), true));
    l.push(cap(t, 7.8, 11.8, "Every lane reads the same context window."));

    // 3. The race: one question, four lanes; first real answer wins, later ones speak only if they add something.
    let q = "\u{201c}How deep is the Mariana Trench?\u{201d}";
    l.push(text(PANEL.0 + VAL, ROWS[1] + 8.0, 26.0, &typed(t, 12.0, 28.0, q), INK, SANS, "start", window(t, 12.0, 22.9, 0.2)));
    let (lane_bottom, rowbb) = (LANE_Y + 72.0, ROWS[2] - 6.0);
    l.push(pulse(t, 13.2, 13.95, LANES[0].x, lane_bottom, PANEL.0 + VAL + 20.0, rowbb, FAST));
    l.push(chip(LANES[0].x - 56.0, LANE_Y + 132.0, "1.3 s", FAST, window(t, 13.3, 22.0, 0.25)));
    l.push(text(PANEL.0 + VAL, ROWS[2] + 8.0, 26.0, &typed(t, 13.95, 60.0, "fast: About 11 km down, at Challenger Deep."), &rgb(FAST), SANS, "start", window(t, 13.95, 18.9, 0.2)));
    l.push(pulse(t, 13.95, 14.4, PANEL.0, PANEL.1 + 220.0, vx + 120.0, vy + 40.0, FAST));
    l.push(chip(LANES[1].x - 92.0, LANE_Y + 132.0, "adds nothing", MEDIUM, window(t, 15.6, 22.0, 0.25)));
    l.push(chip(LANES[2].x - 92.0, LANE_Y + 132.0, "adds nothing", SLOW, window(t, 16.8, 22.0, 0.25)));
    l.push(pulse(t, 18.2, 18.95, LANES[3].x, LANE_Y + 86.0, PANEL.0 + VAL + 20.0, rowbb, HERMES));
    l.push(chip(LANES[3].x - 64.0, LANE_Y + 150.0, "+ new fact", HERMES, window(t, 18.4, 22.0, 0.25)));
    l.push(text(PANEL.0 + VAL, ROWS[2] + 8.0, 26.0, &typed(t, 19.0, 60.0, "Hermes: 10,935 m, sonar-confirmed in 2021."), &rgb(HERMES), SANS, "start", window(t, 19.0, 24.9, 0.2)));
    l.push(pulse(t, 19.6, 20.2, PANEL.0, PANEL.1 + 220.0, vx + 120.0, vy + 40.0, HERMES));
    l.push(cap(t, 12.3, 15.8, "The fastest real answer is spoken first."));
    l.push(cap(t, 16.0, 19.4, "Later lanes speak only if they add something."));
    l.push(cap(t, 19.6, 22.0, "Hermes does the real work, and lands last."));

    // 4. Two voices: Grok takes the floor by name; its words join the shared context.
    let gx = lerp(400.0, 400.0, duet);
    let gy = 760.0;
    let grok_o = window(t, 22.4, 29.4, 0.5);
    l.push(talking(t + 3.0, gx, gy, 200.0, GROK, grok_floor, grok_o));
    l.push(text(gx, gy + 150.0, 22.0, "VOICE · GROK", &rgb(GROK), MONO, "middle", grok_o));
    l.push(text(PANEL.0 + VAL, ROWS[1] + 8.0, 26.0, &typed(t, 23.2, 28.0, "\u{201c}Grok, what's your favorite planet?\u{201d}"), INK, SANS, "start", window(t, 23.0, 30.0, 0.2) * 1.0));
    l.push(chip(gx - 196.0, gy - 128.0, "floor · 2.4 s after its name", GROK, window(t, 25.0, 28.4, 0.25)));
    l.push(text(PANEL.0 + VAL, ROWS[2] + 8.0, 26.0, &typed(t, 25.4, 50.0, "Grok said: Jupiter. The big show-off."), &rgb(GROK), SANS, "start", window(t, 25.4, 30.0, 0.2)));
    l.push(pulse(t, 26.4, 27.2, gx + 110.0, gy - 10.0, PANEL.0, PANEL.1 + 260.0, GROK));
    l.push(cap(t, 22.6, 25.2, "Call a second voice by name."));
    l.push(cap(t, 25.4, 29.2, "Each voice hears what the other said."));

    // 5. End card.
    if end > 0.0 {
        l.push(fframes::svgr!(<rect width="1920" height="1080" fill={PIT.to_owned()} opacity={end * 0.94}/>));
        l.push(orb(t, BREATHING, 960.0, 400.0, 230.0, VOICE, end));
        l.push(rise(t, 30.0, 960.0, 640.0, 120.0, "z0live", INK, SERIF, "middle"));
        l.push(rise(t, 30.4, 960.0, 708.0, 34.0, "Dessa-level conversation. Hermes-level intelligence.", MUTE, SANS, "middle"));
        l.push(rise(t, 30.9, 960.0, 790.0, 28.0, "hermes plugins install https://github.com/kvnloo/z0live", ACCENT, MONO, "middle"));
    }
    l.push(text(1880.0, 1062.0, 16.0, "ORBS: REAL thinking-orbs ENGINE · LATENCIES FROM REAL z0live TRACES · EXAMPLE ANSWERS SCRIPTED", "#5d6663", MONO, "end", 1.0));
    fframes::svgr!(<g>{l}</g>)
}

fn lane_state(t: f32, i: usize) -> u8 {
    let start = 12.6;
    let done = [13.3, 15.6, 16.8, 18.4][i];
    if t >= start && t < done { [WORKING, SEARCHING, SOLVING, SEARCHING][i] } else { BREATHING }
}

fn cap(t: f32, a: f32, b: f32, s: &str) -> Svgr<'static> {
    let o = window(t, a, b, 0.3);
    if o <= 0.0 { return Svgr::empty(); }
    fframes::svgr!(<g opacity={o}>
        <rect x="260" y="968" width="1400" height="64" rx="32" fill="#000000" opacity="0.45"/>
        {text(960.0, 1012.0, 38.0, s, INK, SERIF, "middle", 1.0)}
    </g>)
}

struct Film;
impl Video for Film {
    const FPS: usize = 60;
    const WIDTH: usize = 1920;
    const HEIGHT: usize = 1080;
    const BACKGROUND_COLOR: Color = Color::BLACK;
    fn duration(&self) -> Duration<'_> { Duration::Frames((LEN * 60.0).round() as usize) }
    fn audio(&self) -> AudioMap<'_> { AudioMap::none() }
    fn define_scenes(&self) -> Scenes<'_> { Scenes::from(Vec::<&dyn fframes::Scene>::new()) }
    fn render_frame<'a>(&'a self, frame: Frame, _ctx: &FFramesContext<'a, '_>) -> Svgr<'a> {
        let t = frame.global_index as f32 / 60.0;
        fframes::svgr!(<svg width="1920" height="1080">{film(t)}</svg>)
    }
}

fn main() -> std::process::ExitCode {
    eprintln!("z0live-lanes-v1 ({LEN} s)");
    let media = Media::new().expect("media");
    let folder = Path::new(env!("CARGO_MANIFEST_DIR")).join("dynamic_media");
    let dir = MediaDirectory::read_folder(&folder).expect("dynamic_media");
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

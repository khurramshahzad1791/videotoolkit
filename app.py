"""
Video toolkit — Streamlit version
==================================================
Two tools in one app, usable from your phone once deployed:
  1. Bulk-download Pexels stock video clips by keyword
  2. Generate narration audio with free Microsoft Edge voices (edge-tts)

LOCAL RUN (on your computer, to test before deploying)
    pip install -r requirements.txt
    streamlit run app.py

DEPLOY (free, so you can use it on mobile)
    1. Push this folder to a GitHub repo (app.py + requirements.txt)
       -- make the repo PRIVATE since your Pexels key is hardcoded below.
    2. Go to https://share.streamlit.io -> "New app" -> pick your repo
    3. Deploy. You'll get a URL like https://yourapp.streamlit.app
       Open that URL on your phone -- it just works, like any website.

KEYWORD FORMAT (Stock videos tab)
    One line per keyword. Optionally add a count after a pipe:
        two people talking calm | 3
        plant growing timelapse | 1
        person thinking closeup
    A line with no count uses the default "clips per keyword" value.
"""

import asyncio
import io
import re
import zipfile

import requests
import streamlit as st
import edge_tts

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"

# Your Pexels API key is baked in below so you never have to type it.
# IMPORTANT: keep this GitHub repo PRIVATE, since anyone who can see your
# code can see this key. If you ever make the repo public, remove this
# line and use Streamlit Secrets instead (see README.md).
HARDCODED_API_KEY = "Mjvv82oXXLecpK2G0lqciFctQfSPjwPFHAHDgnmHrgcRg4Z8kI2SS0YK"

VOICE_OPTIONS = {
    "Jenny (US, female, warm)": "en-US-JennyNeural",
    "Aria (US, female, upbeat)": "en-US-AriaNeural",
    "Guy (US, male, calm)": "en-US-GuyNeural",
    "Sonia (UK, female)": "en-GB-SoniaNeural",
    "Ryan (UK, male)": "en-GB-RyanNeural",
    "Natasha (Australia, female)": "en-AU-NatashaNeural",
    "Neerja (India, female)": "en-IN-NeerjaNeural",
    "Prabhat (India, male)": "en-IN-PrabhatNeural",
}

st.set_page_config(page_title="Video toolkit", page_icon="🎬", layout="centered")


def safe_folder_name(text):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "clip"


def pick_best_video_file(video_files):
    if not video_files:
        return None
    hd = [f for f in video_files if f.get("quality") == "hd"]
    pool = hd if hd else video_files
    pool = sorted(pool, key=lambda f: f.get("width", 0), reverse=True)
    return pool[0]


def parse_keyword_lines(raw_text, default_count):
    """Each line: 'keyword' or 'keyword | count'. Returns list of (keyword, count)."""
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    parsed = []
    for line in lines:
        if "|" in line:
            kw, count_str = line.split("|", 1)
            kw = kw.strip()
            try:
                count = max(1, min(10, int(count_str.strip())))
            except ValueError:
                count = default_count
        else:
            kw = line.strip()
            count = default_count
        if kw:
            parsed.append((kw, count))
    return parsed


def get_api_key():
    key = st.secrets.get("PEXELS_API_KEY", "") if hasattr(st, "secrets") else ""
    if not key:
        key = HARDCODED_API_KEY
    return key


async def _generate_tts(text, voice, rate_str):
    communicator = edge_tts.Communicate(text, voice, rate=rate_str)
    audio_bytes = b""
    async for chunk in communicator.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]
    return audio_bytes


def generate_voiceover(text, voice, rate_percent):
    rate_str = f"{rate_percent:+d}%"
    return asyncio.run(_generate_tts(text, voice, rate_str))


async def _generate_tts_with_words(text, voice, rate_str):
    """Generate audio and collect per-word timing (in 100-nanosecond ticks)."""
    communicator = edge_tts.Communicate(text, voice, rate=rate_str)
    audio_bytes = b""
    word_events = []
    async for chunk in communicator.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            word_events.append(chunk)
    return audio_bytes, word_events


def generate_voiceover_with_captions(text, voice, rate_percent, words_per_caption=6):
    rate_str = f"{rate_percent:+d}%"
    audio_bytes, word_events = asyncio.run(_generate_tts_with_words(text, voice, rate_str))
    srt_text = build_srt(word_events, words_per_caption)
    return audio_bytes, srt_text


def _ticks_to_srt_timestamp(ticks):
    # edge-tts timing is in 100-nanosecond ticks -> seconds = ticks / 10,000,000
    total_ms = int(ticks / 10_000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def build_srt(word_events, words_per_caption):
    """Group word-boundary events into readable multi-word caption lines."""
    if not word_events:
        return ""

    lines = []
    index = 1
    for i in range(0, len(word_events), words_per_caption):
        group = word_events[i : i + words_per_caption]
        start_ticks = group[0]["offset"]
        end_ticks = group[-1]["offset"] + group[-1]["duration"]
        text_line = " ".join(w["text"] for w in group)
        lines.append(
            f"{index}\n{_ticks_to_srt_timestamp(start_ticks)} --> {_ticks_to_srt_timestamp(end_ticks)}\n{text_line}\n"
        )
        index += 1
    return "\n".join(lines)


st.title("🎬 Video toolkit")
st.caption("Grab stock clips, generate narration, and get synced captions, all from your phone.")

tab_videos, tab_voice, tab_captions = st.tabs(["📹 Stock videos", "🎙️ Voiceover", "📝 Captions"])

# ============================== TAB 1: STOCK VIDEOS ==============================
with tab_videos:
    st.caption("Type your keywords, set how many clips each one needs, download a zip of everything.")

    api_key = get_api_key()
    if not api_key:
        api_key = st.text_input(
            "Pexels API key",
            type="password",
            help="Get a free key at pexels.com/api.",
        )

    st.markdown("**Keywords** — one per line. Add `| number` to set a custom count for that line.")
    default_keywords = (
        "two people talking calm | 3\n"
        "plant growing timelapse | 1\n"
        "person thinking closeup | 2\n"
        "person taking deep breath | 2\n"
        "curious looking face | 1\n"
        "person saying no hand gesture | 2\n"
        "person standing up recovering | 2\n"
        "confident smile sunrise | 2\n"
    )
    keywords_raw = st.text_area("Keywords", value=default_keywords, height=220, label_visibility="collapsed")

    default_count = st.slider(
        "Default clips per keyword (used for lines without a number)",
        min_value=1,
        max_value=10,
        value=2,
    )

    start = st.button("Download all", type="primary", use_container_width=True, key="video_start")

    if start:
        if not api_key:
            st.error("Enter your Pexels API key above.")
            st.stop()

        keyword_list = parse_keyword_lines(keywords_raw, default_count)
        if not keyword_list:
            st.error("Add at least one keyword.")
            st.stop()

        total_clips_target = sum(count for _, count in keyword_list)
        progress_bar = st.progress(0)
        status = st.empty()
        log_box = st.container(height=250)

        headers = {"Authorization": api_key}
        done = 0
        downloaded = 0
        failed = 0
        fetched_clips = []  # list of (keyword, filename, bytes)

        for keyword, count in keyword_list:
            folder = safe_folder_name(keyword)
            log_box.write(f"🔍 Searching: **{keyword}** (wants {count})")

            try:
                resp = requests.get(
                    PEXELS_SEARCH_URL,
                    headers=headers,
                    params={"query": keyword, "per_page": count, "orientation": "landscape"},
                    timeout=20,
                )
            except requests.RequestException as e:
                log_box.write(f"  ⚠️ Network error: {e}")
                failed += count
                done += count
                progress_bar.progress(min(done / total_clips_target, 1.0))
                continue

            if resp.status_code == 401:
                st.error("API key rejected. Double check it at pexels.com/api.")
                st.stop()
            if resp.status_code == 429:
                st.error("Rate limit hit (Pexels free tier: 200 requests/hour). Try again later.")
                st.stop()
            if resp.status_code != 200:
                log_box.write(f"  ⚠️ Unexpected error {resp.status_code}")
                failed += count
                done += count
                progress_bar.progress(min(done / total_clips_target, 1.0))
                continue

            videos = resp.json().get("videos", [])
            if not videos:
                log_box.write("  ⚠️ No results found.")
                done += count
                progress_bar.progress(min(done / total_clips_target, 1.0))
                continue

            for i, video in enumerate(videos[:count], start=1):
                best_file = pick_best_video_file(video.get("video_files", []))
                if not best_file:
                    failed += 1
                    done += 1
                    progress_bar.progress(min(done / total_clips_target, 1.0))
                    continue

                try:
                    r = requests.get(best_file["link"], timeout=60)
                    r.raise_for_status()
                    filename = f"{folder}_{i:02d}.mp4"
                    fetched_clips.append((keyword, filename, r.content))
                    downloaded += 1
                    log_box.write(f"  ✅ Ready: {filename}")
                except requests.RequestException as e:
                    log_box.write(f"  ⚠️ Failed clip {i}: {e}")
                    failed += 1

                done += 1
                progress_bar.progress(min(done / total_clips_target, 1.0))

        status.success(f"Done. Fetched {downloaded} clips, {failed} failed.")

        if fetched_clips:
            st.markdown("### 📥 Tap each clip to save it to your phone")
            st.caption(
                "Each button saves one video straight to your phone's Downloads/Files app. "
                "To move a clip into Photos/Gallery afterward: open it from Files, Share → Save to Photos."
            )
            current_keyword = None
            for keyword, filename, video_bytes in fetched_clips:
                if keyword != current_keyword:
                    st.markdown(f"**{keyword}**")
                    current_keyword = keyword
                st.download_button(
                    label=f"⬇️ {filename}",
                    data=video_bytes,
                    file_name=filename,
                    mime="video/mp4",
                    use_container_width=True,
                    key=filename,
                )

            st.divider()
            st.markdown("**Prefer one download instead of many taps?**")
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for keyword, filename, video_bytes in fetched_clips:
                    zf.writestr(filename, video_bytes)
            st.download_button(
                label="📦 Download all as one .zip",
                data=zip_buffer.getvalue(),
                file_name="pexels_clips.zip",
                mime="application/zip",
                use_container_width=True,
            )

# ============================== TAB 2: VOICEOVER (TTS) ==============================
with tab_voice:
    st.caption("Paste your script, pick a voice, get an MP3 back. Free Microsoft Edge voices, no API key needed.")

    script_text = st.text_area(
        "Your script",
        value=(
            "Have you ever met someone who just gets it? That's emotional intelligence, "
            "and it's something you build."
        ),
        height=180,
    )

    col1, col2 = st.columns(2)
    with col1:
        voice_label = st.selectbox("Voice", list(VOICE_OPTIONS.keys()))
    with col2:
        speed = st.slider("Speaking speed", min_value=-30, max_value=30, value=0, format="%d%%")

    generate = st.button("Generate voiceover", type="primary", use_container_width=True, key="tts_start")

    if generate:
        if not script_text.strip():
            st.error("Paste some script text first.")
            st.stop()

        with st.spinner("Generating audio..."):
            try:
                voice_id = VOICE_OPTIONS[voice_label]
                audio_bytes = generate_voiceover(script_text.strip(), voice_id, speed)
            except Exception as e:
                st.error(f"Something went wrong generating audio: {e}")
                st.stop()

        if not audio_bytes:
            st.error("No audio was generated. Try again, or try a shorter script.")
            st.stop()

        st.success("Voiceover ready.")
        st.audio(audio_bytes, format="audio/mp3")
        st.download_button(
            label="⬇️ Download narration.mp3",
            data=audio_bytes,
            file_name="narration.mp3",
            mime="audio/mp3",
            use_container_width=True,
        )
        st.caption(
            "Tip: for a multi-scene video, generate one short clip per scene instead of one long file — "
            "it's much easier to line each clip up with its matching image/video in CapCut."
        )

# ============================== TAB 3: CAPTIONS ==============================
with tab_captions:
    st.caption(
        "Paste the same script you used for your voiceover, pick the same voice, and get back "
        "an audio file plus a perfectly time-synced .srt subtitle file — ready to import into CapCut."
    )

    caption_script = st.text_area(
        "Your script",
        value=(
            "Have you ever met someone who just gets it? That's emotional intelligence, "
            "and it's something you build."
        ),
        height=180,
        key="caption_script",
    )

    col1, col2 = st.columns(2)
    with col1:
        caption_voice_label = st.selectbox("Voice", list(VOICE_OPTIONS.keys()), key="caption_voice")
    with col2:
        caption_speed = st.slider(
            "Speaking speed", min_value=-30, max_value=30, value=0, format="%d%%", key="caption_speed"
        )

    words_per_caption = st.slider(
        "Words per caption line",
        min_value=2,
        max_value=12,
        value=6,
        help="Lower = shorter, punchier captions (good for Shorts/Reels). Higher = fewer, longer subtitle lines.",
    )

    generate_captions = st.button(
        "Generate audio + captions", type="primary", use_container_width=True, key="caption_start"
    )

    if generate_captions:
        if not caption_script.strip():
            st.error("Paste some script text first.")
            st.stop()

        with st.spinner("Generating audio and captions..."):
            try:
                voice_id = VOICE_OPTIONS[caption_voice_label]
                cap_audio_bytes, srt_text = generate_voiceover_with_captions(
                    caption_script.strip(), voice_id, caption_speed, words_per_caption
                )
            except Exception as e:
                st.error(f"Something went wrong: {e}")
                st.stop()

        if not cap_audio_bytes or not srt_text:
            st.error("Generation failed. Try again, or try a shorter script.")
            st.stop()

        st.success("Audio and captions ready.")
        st.audio(cap_audio_bytes, format="audio/mp3")

        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            st.download_button(
                label="⬇️ narration.mp3",
                data=cap_audio_bytes,
                file_name="narration.mp3",
                mime="audio/mp3",
                use_container_width=True,
            )
        with dl_col2:
            st.download_button(
                label="⬇️ captions.srt",
                data=srt_text,
                file_name="captions.srt",
                mime="text/srt",
                use_container_width=True,
            )

        with st.expander("Preview caption timing"):
            st.text(srt_text)

        st.caption(
            "In CapCut: import narration.mp3 as your audio track, then use "
            "Captions → Import subtitle (or Text → Import .srt) to bring in captions.srt, "
            "already synced to the voiceover."
        )

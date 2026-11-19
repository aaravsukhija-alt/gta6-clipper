# GTA 6 Auto-Clipper (no API keys)

Every 3 hours a free GitHub cloud server:
1. checks your chosen YouTube and Twitch creators for **new GTA 6 videos and finished streams**, plus any links you paste into `links.txt`;
2. gets the **transcript**: YouTube captions, or, when a video has none (Twitch VODs), a speech-to-text model (Whisper) that runs on the server itself;
3. finds the **best GTA 6 moments** in the transcript (GTA 6 words, reactions like "no way" or "oh my god", [Laughter], and YouTube's "Most replayed" graph);
4. downloads **only those parts** and edits them into clips:
   - a vertical 9:16 version with a title, burned-in captions and a credit line;
   - the original horizontal cut;
5. gives you the clips as a download on the GitHub run page, with a summary of where each clip came from.

No API keys, no logins and no paid services are used. Everything comes from public pages, through [yt-dlp](https://github.com/yt-dlp/yt-dlp). Nothing is posted anywhere; you decide what to upload.

```
config.yaml channels ─┐                  ┌─ YouTube captions ─┐
links.txt links ──────┼─► new video? ───►┤                    ├─► score moments ─► cut ─► edit ─► download
                      │   (GTA 6 title)  └─ Whisper (no caps) ┘
state/processed.json ─┘  remembers what's done, so nothing is clipped twice
```

## Setup (one time)
1. **Add creators.** Edit `config.yaml` on github.com (pencil icon). For each creator, set `platform` (`youtube` or `twitch`), `channel`, `credit`, and `permission: true`.
2. Optional: **paste links** into `links.txt`, one per line. Each one is clipped on the next run.
3. Optional, but **needed for YouTube**: set up your own PC as the runner. See "Run on your own PC" below.

## Getting your clips (phone or PC)
1. Open **github.com/aaravsukhija-alt/gta6-clipper** and tap **Actions**, then **GTA 6 clipper**.
   - On a phone, use the browser (the GitHub app can't download these files). Tap **"Desktop site"** if the page looks cut off.
2. Tap the newest run with a green ✓.
3. The **Summary** lists every clip: its source link (with the timestamp), why it was picked, and what was said.
4. Scroll to **Artifacts** and tap `gta6-clips-<number>` to download a zip of the mp4 files. Unzip it; on iPhone, open it in the Files app.
   - `*_vertical.mp4` is the vertical version for Shorts, Reels and TikTok.
   - `*_original.mp4` is the plain horizontal cut.
   - Downloads are kept for 30 days.
- **Clip a video now:** go to **Actions → GTA 6 clipper → Run workflow**, paste the link into `url`, and Run.

## Run on your own PC (makes YouTube work)
YouTube blocks GitHub's cloud servers ("Sign in to confirm you're not a bot") but not home internet. A *self-hosted runner* is GitHub's free program that lets your PC do the 3-hour job. No API keys or cookies are needed. Twitch works either way.

The PC must be on and awake when a run is due. If it's off, the run waits and starts when the PC comes back. Nothing is lost.

### Windows
1. Open **PowerShell** (Start menu → type `PowerShell` → open it). Run these one at a time and accept any prompts:
   ```
   winget install -e --id Git.Git
   winget install -e --id Python.Python.3.12
   winget install -e --id Gyan.FFmpeg
   ```
2. **Close PowerShell and open it again.** Check that both of these print a version:
   ```
   python --version
   ffmpeg -version
   ```
3. On github.com, open the repo, then **Settings → Actions → Runners → New self-hosted runner** and pick **Windows**.
   - GitHub shows a list of commands that include a secret one-time token. Copy and run them **one by one** in PowerShell. They create a folder `C:\actions-runner`, download the runner, and run `./config.cmd ...`.
   - When `config.cmd` asks questions, press **Enter** for each to accept the defaults.
   - When it asks **"run as service?"**, type **N**. The service account can't see the Python and ffmpeg you just installed.
4. Start the runner:
   ```
   cd C:\actions-runner
   ./run.cmd
   ```
   Leave this window open (you can minimize it). It shows **"Listening for Jobs"**.
5. Optional, to start it automatically at login: press **Win+R**, type `shell:startup`, and press Enter. In that folder, right-click → **New → Shortcut** and enter `C:\actions-runner\run.cmd`.

### Mac
1. Open **Terminal** (Cmd+Space → `Terminal`). Install Homebrew with the one-line command from https://brew.sh (follow the "Next steps" it prints). Then run:
   ```
   brew install python@3.12 ffmpeg git
   ```
2. On github.com, open the repo, then **Settings → Actions → Runners → New self-hosted runner** and pick **macOS**.
   - Pick **ARM64** for M1/M2/M3/M4 Macs, or **x64** for Intel Macs.
   - Run the commands GitHub shows, one by one. Press **Enter** at each question during `./config.sh`.
3. Make it start automatically:
   ```
   cd ~/actions-runner
   ./svc.sh install
   ./svc.sh start
   ```
4. Keep the Mac from sleeping while plugged in: **System Settings → Battery/Energy → Prevent automatic sleeping when the display is off**.

### Last step (both)
On github.com, open the repo, then **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:
- Name: `CLIPPER_RUNNER`
- Value: `self-hosted`

From now on, every run happens on your PC.
- **Test it:** go to **Actions → GTA 6 clipper → Run workflow**, tick **self_test**, and Run. All lines should say PASS.
- **Switch back to the cloud:** delete the variable.

## How moments are picked
- A video is used if its title has a GTA 6 keyword. With `scan_all_videos: true`, every video is checked, and a moment only counts if "GTA 6" is actually said.
- Each 45-second window gets points:
  - GTA 6 keyword ×3;
  - context words (Vice City, Lucia, wanted level, heist...) ×1;
  - reactions (no way, bro, oh my god...) ×1;
  - [Laughter] or [Applause] ×2;
  - "Most replayed" peak ×6.
- The top 3 windows with a score of 4 or more become clips. Clip edges snap to whole sentences, and clips last 15–59 seconds.
- You can change every word list and number in `config.yaml`.

## Timing
- **Streams still live** are skipped and picked up after they end.
- **Fresh YouTube uploads** often have no auto-captions for a few hours. The clipper waits up to 2 runs (about 6 hours) for them, then uses Whisper instead.
- **Twitch VODs** have no captions, so they always use Whisper. A 4-hour stream takes roughly 20–40 minutes on the free server. Streams longer than `whisper_max_minutes` are skipped.
- Each run handles at most `max_videos_per_run` (4) videos. Any extra videos wait for the next run.

## Music and copyright
- The clipper **never adds music**. Clips only contain the streamer's own audio.
- That audio can still include copyrighted music: in-game radio, or music the streamer plays. YouTube and Instagram may then claim or mute the clip.
- Before posting, listen for background songs and skip those clips.
- Only clip creators who said OK, and credit them. Reuploading without permission gets copyright strikes.

## Limits
- **Free minutes:** public repos get unlimited Actions minutes; private repos get 2,000 per month. Runs on your own PC don't use any minutes.
- GitHub pauses schedules in repos with **no activity for 60 days**. Just push any change, or click "Enable workflow".
- **Optional cookies:** if you want YouTube to work in the cloud instead of on your PC, save a `cookies.txt` export from a spare Google account as the secret `YT_COOKIES`. Cookies expire every few weeks.

## Commands (on your own PC, without GitHub)
```
pip install -r requirements.txt      # also needs ffmpeg and deno installed
python -m clipper.run                # or:  python -m clipper.run --url <link>
python -m pytest -q tests            # offline tests
python tests/live_check.py           # checks YouTube/Twitch/Whisper are reachable
```
Clips appear in `output/<date_time>/`.

## Files
| File | What it does |
|---|---|
| `config.yaml` | creators, keywords, clip settings |
| `links.txt` | extra links to clip |
| `state/processed.json` | memory of handled videos (saved by the workflow) |
| `clipper/run.py` | the whole run: discover → transcript → select → cut → summary |
| `clipper/transcript.py` | YouTube captions (json3/vtt) and Whisper fallback |
| `clipper/selector.py` | moment scoring |
| `clipper/editor.py` | ffmpeg vertical edit and captions |
| `clipper/ytdlp.py` | every download (public pages only) |
| `.github/workflows/gta6-clipper.yml` | the 3-hour schedule on GitHub's servers |

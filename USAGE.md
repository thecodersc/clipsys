# clipsys for Windows: how to use it

clipsys turns long videos into short vertical clips with subtitles and captions, lets you
approve them, and uploads the approved ones to YouTube Shorts.

Everything runs on your own PC. Nothing is uploaded until you approve a clip and run
`publish --live`.

---

## 1. What you need

| Requirement | Why | How to get it |
|---|---|---|
| Windows 10 or 11, 64-bit | | |
| ffmpeg | cutting and rendering video | `winget install --id Gyan.FFmpeg -e` |
| Ollama + the `qwen3` model | writing titles and descriptions | `winget install --id Ollama.Ollama -e`, then `ollama pull qwen3:latest` (about 5 GB) |
| A Google Cloud project | YouTube only accepts uploads from a registered app | free, see step 3 |
| About 10 GB of free disk space | models, clips | |

An NVIDIA graphics card is optional. Without one, transcription runs on the processor and
is slower. See "GPU add-on" near the end.

After installing ffmpeg and Ollama, **close and reopen PowerShell** so it can find them.

---

## 2. Install clipsys

1. Download `clipsys-windows.zip` and unzip it, for example to `C:\clipsys`.
   You get a folder containing `clipsys.exe`, a `config` folder, an `inbox` folder and this file.
2. In that folder, copy `.env.example` to a new file named `.env`.
3. Open PowerShell in the folder (Shift + right-click in the folder, "Open PowerShell window here")
   and check the setup:

```powershell
.\clipsys.exe doctor
```

Every row should say PASS. "YT tokens: WARN" is expected until step 4.

Do not double-click `clipsys.exe`. It is a command-line program, so the window would open and
close straight away. Always run it from PowerShell.

Windows may show a "Windows protected your PC" warning the first time, because the program is
not signed. Click **More info**, then **Run anyway**.

---

## 3. Create your YouTube API credentials (once)

1. Go to <https://console.cloud.google.com>, create a project (or pick one).
2. **APIs & Services > Library**: find **YouTube Data API v3** and click **Enable**.
3. **APIs & Services > OAuth consent screen**: app name of your choice, audience **External**.
   Under **Audience > Test users**, add the Google account that owns your YouTube channel.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**.
   Application type: **Desktop app**. Create it and copy the **Client ID** and **Client secret**.
5. Open `.env` in Notepad and paste them in, with nothing around the values:

```
YOUTUBE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=GOCSPX-xxxxxxxx
```

Keep `.env` private. Anyone with these values can act as your app.

Tip: while the consent screen is in "Testing", the sign-in expires every 7 days and you repeat
step 4. Switching it to "In production" on the Audience page removes that limit.

---

## 4. Connect your YouTube channel (once)

1. Open `config\accounts.yaml` in Notepad. The first account, `acct_01`, is the one that posts.
   Change its `handle` to your channel's handle. It is already set to `live: true`.
2. Sign in:

```powershell
.\clipsys.exe auth acct_01
```

A browser opens. Pick the Google account of your channel. On the "Google hasn't verified this
app" page click **Advanced**, continue, tick both YouTube permissions and allow. The terminal
prints `Token saved`.

---

## 5. Make clips

1. Copy your video files into the `inbox` folder.
2. Open `inbox\manifest.csv` in Notepad and add one line per video:

```
file,program_id,creator,source_url_or_empty,received_at
myvideo.mp4,self_owned_test,me,,2026-11-18
```

   - `file`: the exact file name in `inbox`
   - `program_id`: which permission record covers this video (see "Rights" below)
   - `creator`: who made the video
   - `source_url_or_empty`: the YouTube link of the original, or leave empty
   - `received_at`: today's date

3. Run the pipeline. It transcribes, finds the best moments, cuts, renders and captions.
   Nothing is uploaded.

```powershell
.\clipsys.exe run-all
```

The first run downloads the speech model (about 1.5 GB). A one-hour video takes a few minutes
with an NVIDIA card and much longer without.

Finished clips are in `data\variants`.

---

## 6. Approve and post

1. Start the review page:

```powershell
.\clipsys.exe review
```

2. Open <http://127.0.0.1:8000> in your browser. Watch each clip, edit the title, description
   or hashtags if you want, and click **Approve** or **Reject**.
3. Go back to PowerShell and press **Ctrl+C** to stop the review page.
4. Upload the approved clips:

```powershell
.\clipsys.exe publish --live
```

Always include `--live`. Without it, clipsys does a practice run that marks the approved clips
as posted without uploading them.

Each clip is uploaded as private and then switched to public. If the switch fails you are told,
and you can make that video public yourself in YouTube Studio.

---

## 7. Daily limits

- **6 uploads a day.** YouTube gives each Google Cloud project 10,000 quota units a day and one
  upload costs 1,650. Extra approved clips wait and go up the next time you run
  `publish --live` on a later day.
- **6 new clips a day are prepared** for review, best first. If a video produced more, run
  `run-all` again on the following days to get the next batch.

Check where things stand at any time:

```powershell
.\clipsys.exe status
```

---

## 8. Settings you may want to change

All settings are plain text files in the `config` folder. Edit them in Notepad.

| File | Setting | What it does |
|---|---|---|
| `thresholds.yaml` | `min_score` | Lower it (for example 0.3) for more clips per video, raise it for fewer and better ones |
| `thresholds.yaml` | `clips: short: min_s / max_s` | Clip length in seconds |
| `accounts.yaml` | `daily_cap` | Clips per day for that account (6 is the YouTube maximum) |
| `accounts.yaml` | `layout` | `blur` shows the whole picture over a blurred background; `crop` fills the screen and cuts the sides off |
| `accounts.yaml` | `zoom` | With `blur`: 1.0 shows the full width, 1.4 makes the picture bigger and trims the sides |
| `personas\acct_01.md` | | The writing style and rules for titles and descriptions |
| `programs.yaml` | | Permission records and credit lines, see below |

---

## 9. Rights: whose footage is it?

clipsys refuses to ingest a video unless its `program_id` exists in `config\programs.yaml`
with a permission reference. This is your record of why you are allowed to post each clip.

- `self_owned_test` is only for videos **you made yourself**.
- For another creator's footage, add a program for them, with the agreement or clipping
  programme you have as `permission_ref`, and a `credit_line`. The credit line is added to the
  description of every clip from that program:

```yaml
  - id: creator_name
    name: Creator Name clipping programme
    credit_line: "Clip from @theirhandle"
    require_link_to_creator: true
    allowed_platforms: [youtube_shorts]
    rights_notes: "What the agreement allows."
    clip_permission: true
    permission_ref: "where the permission is written down"
```

Posting footage you have no rights to can get the video removed and the channel penalised.

---

## 10. All commands

| Command | What it does |
|---|---|
| `.\clipsys.exe doctor` | Checks that everything needed is installed |
| `.\clipsys.exe auth acct_01` | Signs in to YouTube |
| `.\clipsys.exe run-all` | Runs every step from ingest to caption |
| `.\clipsys.exe review` | Starts the approval page |
| `.\clipsys.exe publish --live` | Uploads approved clips |
| `.\clipsys.exe status` | Shows counts and today's quota |
| `.\clipsys.exe record <url>` | Records a live stream into chunks (needs streamlink) |
| `.\clipsys.exe watch <folder>` | Watches a recordings folder and clips each finished file (never uploads) |
| `.\clipsys.exe --help` | Lists every command |

To stop all uploading immediately, create an empty file named `KILL` in the `data` folder.
Delete it to resume.

---

## 11. Clip from a live stream

This needs one more program, streamlink: `winget install Streamlink.Streamlink` (then reopen
PowerShell). You also need a program in `config\programs.yaml` that covers the streamer's
footage, see "Rights" above.

Use two PowerShell windows, both in the clipsys folder.

**Window 1: record the stream.** It is saved into a `recordings` folder in 10-minute chunks.
If the channel is offline, it waits and checks every minute.

```powershell
.\clipsys.exe record https://www.twitch.tv/channelname
```

**Window 2: clip each chunk as it finishes.**

```powershell
.\clipsys.exe watch recordings --program creator_name --creator "Creator Name"
```

Each time a chunk is complete, clipsys transcribes it, finds the best moments, and renders and
captions the clips. Nothing is uploaded: approve and post as in step 6.

- Clips appear roughly one chunk behind the stream. For faster clips use shorter chunks:
  `--segment-minutes 5`.
- Works with Twitch, Kick, YouTube Live and other sites streamlink supports.
- Press **Ctrl+C** in each window to stop. The chunk being recorded is closed properly.
- Recordings are large (about 1.5 GB per hour at 1080p). Delete old files from `recordings`
  once their clips are made.

---

## 12. GPU add-on (optional, NVIDIA cards only)

Transcription is many times faster on an NVIDIA graphics card.

1. Download `clipsys-gpu-addon.zip` (about 1 GB).
2. Unzip it **into the clipsys folder**, so that you end up with `clipsys\gpu\cublas64_12.dll`
   and the other files next to it.

Nothing else to set. clipsys uses the card when the `gpu` folder is there and falls back to the
processor when it is not. To force the processor, put `WHISPER_DEVICE=cpu` in `.env`.

---

## 13. If something goes wrong

| What you see | What to do |
|---|---|
| `doctor` says ffmpeg not found | Install ffmpeg, reopen PowerShell. Or set `FFMPEG_BIN` and `FFPROBE_BIN` in `.env` |
| `doctor` says Ollama unreachable | Start Ollama from the Start menu, then `ollama pull qwen3:latest` |
| "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET" | `.env` is missing or the two lines are empty |
| Browser: "Access blocked ... 403 access_denied" | Add your Google account under Test users (step 3) |
| Sign-in suddenly stops working after a week | Run `auth acct_01` again, or switch the consent screen to "In production" |
| A video file is skipped at ingest | Its `program_id` is not in `programs.yaml`, or the file name in the manifest is wrong |
| A video is uploaded but private | Make it public in YouTube Studio. If it happens every time, your Google project may need YouTube's API audit |
| Anything else | Look at the last lines of `data\logs\clipsys.jsonl`; each error has an `err` field with the reason |

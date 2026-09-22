from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_NAME = "vikky-movie-ai-bot"
DATA_DIR = Path("/data")
JOBS_DIR = DATA_DIR / "jobs"

app = modal.App(APP_NAME)

media_volume = modal.Volume.from_name(
    "vikky-media",
    create_if_missing=True,
)

telegram_secret = modal.Secret.from_name(
    "vikky-telegram",
    required_keys=["TELEGRAM_BOT_TOKEN"],
)

remote_secret = modal.Secret.from_name(
    "vikky-remote",
    required_keys=["VIKKY_REMOTE_TOKEN"],
)

base_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "aria2", "git")
    .pip_install(
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "numpy>=1.24,<3",
        "pydantic-settings>=2.7,<3",
        "pycdlib>=1.14,<2",
        "scipy>=1.11,<2",
        "soundfile>=0.12,<1",
        "requests>=2.31,<3",
        "psutil>=5.9,<8",
    )
    .add_local_python_source("app")
)

gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "ffmpeg",
        "aria2",
        "libgl1",
        "libglib2.0-0",
    )
    .pip_install(
        "torch==2.1.2",
        "torchvision==0.16.2",
        "torchaudio==2.1.2",
    )
    .pip_install(
        "opencv-python-headless>=4.9,<5",
        "numpy>=1.24,<3",
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "psutil>=5.9,<8",
    )
    .pip_install(
        "basicsr>=1.4.2,<2",
        "realesrgan>=0.3,<1",
        extra_options="--no-build-isolation",
    )
    .add_local_python_source("app")
)


def run_async(awaitable: Any) -> Any:
    if not inspect.isawaitable(awaitable):
        return awaitable
    return asyncio.run(awaitable)


def call_flexible(func: Any, *args: Any, **kwargs: Any) -> Any:
    return run_async(func(*args, **kwargs))


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def tg_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token=os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is not available")
    with httpx.Client(timeout=30) as client:
        response=client.post(f"https://api.telegram.org/bot{token}/{method}",json=payload)
        response.raise_for_status(); data=response.json()
    if not data.get("ok"): raise RuntimeError(data.get("description","Telegram API failed"))
    return data

def menu_markup() -> dict[str, Any]:
    return {"keyboard":[["🔄 Audio Sync","📦 x265 Encode (3-5 GiB)"],["🎬 1080p Hybrid Remaster","👑 4K Theater Remaster"],["📊 Cluster Status","❌ Cancel / Reset"]],"resize_keyboard":True,"is_persistent":True}

def cancel_markup(job_id: str) -> dict[str, Any]:
    return {"inline_keyboard":[[{"text":"❌ Cancel Process","callback_data":f"abort_{job_id}"}]]}

def send_tg_message(chat_id:int,text:str,menu:bool=False,inline:dict[str,Any]|None=None)->int|None:
    p={"chat_id":chat_id,"text":text}
    if menu:p["reply_markup"]=menu_markup()
    if inline is not None:p["reply_markup"]=inline
    try:return int((tg_api("sendMessage",p).get("result") or {}).get("message_id"))
    except Exception as exc: print(f"Telegram sendMessage failure: {exc}",file=sys.stderr); return None

def edit_tg_message(chat_id:int,message_id:int|None,text:str,inline:dict[str,Any]|None=None)->None:
    if not message_id:return
    p={"chat_id":chat_id,"message_id":message_id,"text":text}
    if inline is not None:p["reply_markup"]=inline
    try:tg_api("editMessageText",p)
    except Exception as exc:print(f"Telegram edit failure: {exc}",file=sys.stderr)

def answer_callback(callback_id:str,text:str="")->None:
    try:tg_api("answerCallbackQuery",{"callback_query_id":callback_id,"text":text})
    except Exception:pass

def progress_bar(percent:float)->str:
    n=max(0,min(20,round(percent/5))); return "█"*n+"░"*(20-n)

def fmt_time(seconds:float)->str:
    s=max(0,int(seconds)); h,s=divmod(s,3600); m,s=divmod(s,60); return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_speed(bps:float)->str:
    if bps<=0:return "—"
    units=("B/s","KB/s","MB/s","GB/s"); i=0; v=float(bps)
    while v>=1024 and i<3:v/=1024;i+=1
    return f"{v:.1f} {units[i]}"

class LiveUI:
    def __init__(self,chat_id:int,job_id:str,task:str):
        self.chat_id=chat_id; self.job_id=job_id; self.task=task; self.message_id=None
        self.started=time.monotonic(); self.last_edit=0.0; self.last_bytes=0; self.last_sample=self.started
    def render(self,stage:int,status:str,percent:float,speed:float=0,eta:str="—")->str:
        return (f"🎬 Task: {self.task} (Job ID: {self.job_id})\n"
                f"Status: [{stage}/5] {status}\n"
                f"Progress: [{progress_bar(percent)}] {percent:.0f}%\n"
                f"Speed: {fmt_speed(speed)} | Elapsed: {fmt_time(time.monotonic()-self.started)} | ETA: {eta}")
    def start(self):
        self.message_id=send_tg_message(self.chat_id,self.render(1,"📥 Downloading via turbo engine...",0),inline=cancel_markup(self.job_id))
    def update(self,stage:int,status:str,percent:float,current_bytes:int=0,total_bytes:int|None=None,force:bool=False):
        now=time.monotonic(); dt=now-self.last_sample
        speed=(current_bytes-self.last_bytes)/dt if dt>0 and current_bytes>=self.last_bytes else 0
        if current_bytes:self.last_bytes=current_bytes; self.last_sample=now
        if not force and now-self.last_edit<3.2:return
        eta="—"
        if speed>0 and total_bytes and percent>0:eta=fmt_time((total_bytes-total_bytes*percent/100)/speed)
        edit_tg_message(self.chat_id,self.message_id,self.render(stage,status,percent,speed,eta),cancel_markup(self.job_id)); self.last_edit=now
    def final(self,text:str):edit_tg_message(self.chat_id,self.message_id,text,{"inline_keyboard":[]})

class JobCancelled(RuntimeError):pass
def abort_path(job_id:str)->Path:return DATA_DIR/f"abort_{job_id}.flag"
def request_abort(job_id:str):abort_path(job_id).write_text("abort",encoding="utf-8")
def clear_abort(job_id:str):abort_path(job_id).unlink(missing_ok=True)
def kill_children():
    try:
        import psutil
        for p in reversed(psutil.Process(os.getpid()).children(recursive=True)):
            try:p.kill()
            except Exception:pass
    except Exception:pass
def check_abort(job_id:str):
    if abort_path(job_id).exists():kill_children();raise JobCancelled("cancelled")
def abort_watch(job_id:str,stop:threading.Event):
    while not stop.wait(.5):
        if abort_path(job_id).exists():kill_children();return
def start_abort_watch(job_id:str):
    stop=threading.Event();threading.Thread(target=abort_watch,args=(job_id,stop),daemon=True).start();return stop
def run_abortable(job_id:str,command:list[str],timeout:int=7200):
    check_abort(job_id);p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);started=time.monotonic()
    while p.poll() is None:
        if abort_path(job_id).exists():p.kill();p.wait(timeout=5);raise JobCancelled("cancelled")
        if time.monotonic()-started>timeout:p.kill();raise TimeoutError("process timeout")
        time.sleep(.5)
    out,err=p.communicate()
    if p.returncode:raise subprocess.CalledProcessError(p.returncode,command,out,err)
    return subprocess.CompletedProcess(command,0,out,err)


USER_STATE_FILE = DATA_DIR / "user_state.json"


def load_user_states() -> dict[str, dict[str, Any]]:
    try:
        if USER_STATE_FILE.exists():
            raw = json.loads(USER_STATE_FILE.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"State load error: {exc}", file=sys.stderr)
    return {}


def save_user_states(states: dict[str, dict[str, Any]]) -> None:
    USER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(states, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, USER_STATE_FILE)


def set_user_state(chat_id: int, state: dict[str, Any]) -> None:
    states = load_user_states()
    states[str(chat_id)] = state
    save_user_states(states)


def get_user_state(chat_id: int) -> dict[str, Any] | None:
    return load_user_states().get(str(chat_id))


def clear_user_state(chat_id: int) -> None:
    states = load_user_states()
    states.pop(str(chat_id), None)
    save_user_states(states)


def audio_summary(media_info: Any) -> str:
    tracks = getattr(media_info, "tracks", None)
    if tracks is not None:
        values: list[str] = []
        for track in tracks:
            track_type = getattr(
                track,
                "codec_type",
                getattr(track, "track_type", getattr(track, "type", getattr(track, "kind", None))),
            )
            if str(track_type).lower() != "audio":
                continue
            channels = getattr(track, "channels", None)
            layout = getattr(track, "channel_layout", getattr(track, "layout", None))
            sample_rate = getattr(track, "sample_rate", None)
            codec = getattr(track, "codec_name", getattr(track, "codec", None))
            if channels is not None or layout is not None:
                values.append(
                    f"{codec or '?'} {channels or '?'}ch {layout or 'layout-unknown'} "
                    f"{sample_rate or '?'}Hz"
                )
        if values:
            return ", ".join(values)
    return "Preserved"


def video_resolution(media_info: Any) -> str:
    tracks = getattr(media_info, "tracks", None)
    if tracks is None:
        return "Unknown"
    for track in tracks:
        track_type = getattr(
            track,
            "codec_type",
            getattr(track, "track_type", getattr(track, "type", getattr(track, "kind", None))),
        )
        if str(track_type).lower() != "video":
            continue
        width = getattr(track, "width", None)
        height = getattr(track, "height", None)
        if width and height:
            return f"{width}x{height}"
    return "Unknown"


def verify_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 1024 * 1024:
        raise RuntimeError(f"Invalid output: {path}")
    subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def cleanup_scratch(*paths: Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def upload_output(output_path: Path) -> dict[str, str]:
    from app.uploaders import upload_to_all
    result = call_flexible(upload_to_all, Path(output_path))
    if not isinstance(result, dict):
        raise RuntimeError("upload_to_all returned an invalid response")
    successful = {
        str(provider): value.strip()
        for provider, value in result.items()
        if isinstance(value, str) and value.strip() and not value.startswith("ERROR:")
    }
    if not successful:
        raise RuntimeError(f"All upload providers failed: {result}")
    return successful


def format_links(links: dict[str, str]) -> str:
    return "\n".join(f"• {name}: {url}" for name, url in links.items())


def commit_volume(job_id: str) -> None:
    try:
        media_volume.commit()
    except Exception as exc:
        print(f"Volume commit error [{job_id}]: {exc}", file=sys.stderr)


def finish_job_dir(job_dir: Path, output_path: Path) -> None:
    if not output_path.exists() and job_dir.exists():
        try:
            job_dir.rmdir()
        except OSError:
            pass


MEDIA_EXTENSIONS={".mkv",".mp4",".m4v",".mov",".avi",".webm",".ts",".m2ts",".mts",".mp3",".aac",".m4a",".flac",".wav",".ogg",".opus",".zip"}
def is_media_file(p:Path)->bool:return p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
def safe_extract_zip(zip_path:Path,destination:Path,job_id:str)->Path:
    check_abort(job_id);destination.mkdir(parents=True,exist_ok=True);root=destination.resolve()
    with zipfile.ZipFile(zip_path) as z:
        infos=z.infolist()
        if len(infos)>10000 or sum(i.file_size for i in infos)>60*1024**3:raise RuntimeError("ZIP exceeds safe extraction limits")
        for i in infos:
            check_abort(job_id);t=(destination/i.filename).resolve()
            if t!=root and root not in t.parents:raise RuntimeError("Unsafe ZIP path traversal detected")
            if i.is_dir():continue
            t.parent.mkdir(parents=True,exist_ok=True)
            with z.open(i) as src,t.open("wb") as dst:
                while True:
                    check_abort(job_id);chunk=src.read(1024*1024)
                    if not chunk:break
                    dst.write(chunk)
    files=[p for p in destination.rglob("*") if is_media_file(p)]
    if not files:raise RuntimeError("ZIP contains no supported media")
    zip_path.unlink(missing_ok=True);return max(files,key=lambda p:p.stat().st_size)
def resolve_gofile(url:str)->str:
    m=re.search(r"/d/([A-Za-z0-9]+)",url)
    if not m:return url
    try:
        with httpx.Client(timeout=30,follow_redirects=True) as client:data=client.get(f"https://api.gofile.io/contents/{m.group(1)}").json().get("data") or {}
        if isinstance(data.get("link"),str):return data["link"]
        ch=data.get("children") or {}
        files=[v for v in ch.values() if isinstance(v,dict) and v.get("link")] if isinstance(ch,dict) else []
        return str(max(files,key=lambda x:x.get("size",0))["link"]) if files else url
    except Exception:return url
def resolve_platform_url(url:str)->str:
    host=urlparse(url).netloc.lower()
    if "gofile.io" in host:return resolve_gofile(url)
    m=re.search(r"pixeldrain\.com/(?:u|l)/([A-Za-z0-9_-]+)",url)
    if m:return f"https://pixeldrain.com/api/file/{m.group(1)}"
    if "buzzheavier.com" in host or "gdflix" in host:
        with httpx.Client(timeout=45,follow_redirects=True) as client:
            r=client.get(url);r.raise_for_status();direct=_html_download_link(str(r.url),r.text)
            if direct:return direct
    return url
def assert_not_html(path:Path):
    with path.open("rb") as f:head=f.read(4096).lstrip().lower()
    if b"<!doctype" in head or b"<html" in head:raise RuntimeError("❌ Error: Link returned an HTML web page instead of media. Please provide a direct download or stream link.")
def aria2_download(job_id:str,url:str,destination:Path,ui:LiveUI|None=None)->Path:
    check_abort(job_id);destination.parent.mkdir(parents=True,exist_ok=True)
    cmd=["aria2c","--allow-overwrite=true","--auto-file-renaming=false","--continue=true","--max-connection-per-server=16","--split=16","--min-split-size=1M","--file-allocation=none","--summary-interval=3","--console-log-level=warn","--dir",str(destination.parent),"--out",destination.name,url]
    p=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True);last=0;tick=time.monotonic()
    try:
        while p.poll() is None:
            check_abort(job_id);size=destination.stat().st_size if destination.exists() else 0;now=time.monotonic()
            if ui:ui.update(1,"📥 Downloading via turbo engine...",min(18,4+size/(1024**3)*14),size)
            last,tick=size,now;time.sleep(.8)
        err=p.stderr.read() if p.stderr else ""
        if p.returncode:raise subprocess.CalledProcessError(p.returncode,cmd,stderr=err)
    except JobCancelled:
        p.kill();p.wait(timeout=5);raise
    finally:
        if p.poll() is None:p.kill();p.wait(timeout=5)
    if not destination.exists() or destination.stat().st_size<=0:raise RuntimeError("aria2c produced no file")
    assert_not_html(destination);return destination
def download_media(job_id:str,url:str,destination:Path,ui:LiveUI|None=None)->Path:
    if not is_valid_url(url):raise ValueError("Invalid HTTP/HTTPS media URL")
    p=aria2_download(job_id,resolve_platform_url(url),destination,ui)
    return safe_extract_zip(p,destination.parent/"unzipped",job_id) if p.suffix.lower()==".zip" else p

def extract_reference_audio(job_id:str,reference_path:Path,scratch_dir:Path)->Path:
    check_abort(job_id)
    if reference_path.suffix.lower() in {".flac",".wav",".m4a",".aac",".mp3",".ogg",".opus"}:return reference_path
    p=run_abortable(job_id,["ffprobe","-v","error","-select_streams","a","-show_entries","stream=index,codec_name,channels,channel_layout,bit_rate","-of","json",str(reference_path)],300)
    streams=json.loads(p.stdout).get("streams",[])
    if not streams:raise RuntimeError("Reference source contains no audio stream")
    pref={"dts":100,"truehd":95,"flac":90,"eac3":85,"ac3":80}
    best=max(streams,key=lambda s:(pref.get(str(s.get("codec_name") or "").lower(),10)+min(int(s.get("channels") or 0),8)*5,int(s.get("channels") or 0),int(s.get("bit_rate") or 0)))
    out=scratch_dir/"reference_audio.flac"
    run_abortable(job_id,["ffmpeg","-y","-v","error","-i",str(reference_path),"-map",f"0:{best['index']}","-vn","-c:a","flac",str(out)])
    return out

def run_ffmpeg_remaster(job_id: str, source_path: Path, output_path: Path, four_k: bool = False) -> None:
    if four_k:
        vf = "scale=3840:2160:flags=lanczos+accurate_rnd,hqdn3d=1.2:1.2:2.5:2.5,deband=range=20:blur=true:coupling=true,unsharp=5:5:0.65:5:5:0.0,colorbalance=gs=-0.025:gm=-0.01:gb=0.015:ms=-0.015:mm=0.00:mb=0.025:hs=-0.01:hm=0.00:hb=0.01,eq=saturation=1.16:contrast=1.07:brightness=0.01,noise=c1s=4:c1f=t,format=yuv420p10le"
        crf = "17"
    else:
        vf = "scale=1920:1080:flags=lanczos+accurate_rnd,hqdn3d=1.5:1.5:3:3,deband=range=16:blur=true:coupling=true,unsharp=5:5:0.7:5:5:0.0,colorbalance=gs=-0.02:gm=-0.01:gb=0.01:ms=-0.01:mm=0.00:mb=0.02,eq=saturation=1.14:contrast=1.06:brightness=0.01,noise=c1s=3:c1f=t,format=yuv420p10le"
        crf = "18"
    run_abortable(job_id, ["ffmpeg","-y","-v","error","-i",str(source_path),"-map","0:v:0","-map","0:a?","-map","0:s?","-vf",vf,"-c:v","libx265","-crf",crf,"-preset","medium","-pix_fmt","yuv420p10le","-fps_mode","cfr","-c:a","copy","-c:s","copy","-max_muxing_queue_size","4096","-map_metadata","0",str(output_path)], 7200)

def run_encode_worker(job_id: str, chat_id: int, source_url: str) -> None:
    process_encode_task(job_id, chat_id, source_url)


def job_workspace(job_id: str) -> tuple[Path, Path]:
    job_dir = JOBS_DIR / job_id
    scratch = job_dir / "scratch"
    job_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return job_dir, scratch


@app.function(image=base_image,volumes={str(DATA_DIR):media_volume},secrets=[telegram_secret,remote_secret],cpu=8,memory=32768,timeout=7200)
QUEUE_FILE = DATA_DIR / "queue.json"
ACTIVE_FILE = DATA_DIR / "active_job.json"
QUEUE_LOCK = DATA_DIR / ".queue.lock"
QUEUE_LIMIT = 100

def _queue_lock(fn):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            QUEUE_LOCK.mkdir(); break
        except FileExistsError: time.sleep(0.05)
    try: return fn()
    finally: shutil.rmtree(QUEUE_LOCK, ignore_errors=True)

def load_queue() -> list[dict[str, Any]]:
    try:
        value=json.loads(QUEUE_FILE.read_text(encoding="utf-8")); return value if isinstance(value,list) else []
    except Exception: return []

def save_queue(items:list[dict[str,Any]])->None:
    tmp=QUEUE_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(items,ensure_ascii=False),encoding="utf-8"); os.replace(tmp,QUEUE_FILE)

def get_active_job()->dict[str,Any]|None:
    try:
        value=json.loads(ACTIVE_FILE.read_text(encoding="utf-8")); return value if isinstance(value,dict) else None
    except Exception:return None

def enqueue_job(job:dict[str,Any])->int:
    def op():
        items=load_queue()
        if len(items)>=QUEUE_LIMIT: raise RuntimeError("FIFO queue is full")
        items.append(job); save_queue(items); return len(items)
    return _queue_lock(op)

def claim_next_job()->dict[str,Any]|None:
    def op():
        if get_active_job() is not None:return None
        items=load_queue()
        if not items:return None
        job=items.pop(0); ACTIVE_FILE.write_text(json.dumps(job),encoding="utf-8"); save_queue(items); return job
    return _queue_lock(op)

def release_active(job_id:str)->None:
    def op():
        active=get_active_job()
        if active and active.get("job_id")==job_id: ACTIVE_FILE.unlink(missing_ok=True)
    _queue_lock(op)

def queue_position(job_id:str)->int:
    for index,item in enumerate(load_queue(),1):
        if item.get("job_id")==job_id:return index
    return 0

def dispatch_next()->None:
    job=claim_next_job()
    if not job:return
    args=(job["job_id"],int(job["chat_id"]),*job["args"])
    try:
        if job["kind"]=="sync": process_sync_task.spawn(*args)
        elif job["kind"]=="encode_x265": process_encode_task.spawn(*args)
        elif job["kind"]=="remaster_1080p": process_remaster_task.spawn(*args,False)
        elif job["kind"]=="remaster_4k": process_remaster_task.spawn(*args,True)
        else: raise RuntimeError("Unknown queued job type")
    except Exception:
        release_active(job["job_id"]); enqueue_job(job); raise

def finish_job(job_id:str)->None:
    release_active(job_id)
    try: dispatch_next()
    except Exception as exc: print(f"Queue dispatch error: {exc}",file=sys.stderr)

def execute_media_job(job_id:str,chat_id:int,name:str,sources:list[str],processor,filename:str,stage3:str)->None:
    media_volume.reload(); job_dir,scratch=job_workspace(job_id); output=job_dir/filename; ui=LiveUI(chat_id,job_id,name); stop=start_abort_watch(job_id)
    try:
        ui.start()
        if len(sources)==2:
            candidate=download_media(job_id,sources[0],scratch/"candidate",ui); ui.update(2,"🔍 Inspecting media & extracting reference audio...",25,force=True)
            reference=download_media(job_id,sources[1],scratch/"reference",ui); reference_audio=extract_reference_audio(job_id,reference,scratch); ui.update(3,"🎛️ Analyzing Waveforms & Drift...",50,force=True); processor(reference_audio,candidate,output)
        else:
            source=download_media(job_id,sources[0],scratch/"source",ui); ui.update(2,"🔍 Inspecting media...",25,force=True); ui.update(3,stage3,50,force=True); processor(source,output)
        check_abort(job_id); verify_output(output); ui.update(4,"☁️ Uploading output to cloud hosts...",88,force=True); links=upload_output(output)
        ui.final("✅ Process Completed!\n\n"+f"📄 File: {output.name}\n📦 Verification: PASS\n🔗 Download Links:\n"+format_links(links)); output.unlink(missing_ok=True)
    except JobCancelled: cleanup_scratch(scratch); ui.final("🛑 Process Cancelled by User. Scratch disk scrubbed.")
    except Exception as exc: ui.final(f"❌ {name} failed.\n\nJob: {job_id}\nError: {exc}")
    finally: stop.set(); cleanup_scratch(scratch); clear_abort(job_id); commit_volume(job_id); finish_job(job_id)

@app.function(image=base_image,volumes={str(DATA_DIR):media_volume},secrets=[telegram_secret,remote_secret],cpu=8,memory=32768,timeout=7200,max_containers=1)
def process_sync_task(job_id:str,chat_id:int,candidate_url:str,audio_url:str):
    from app.sync.engine import sync_and_verify
    execute_media_job(job_id,chat_id,"Audio Sync",[candidate_url,audio_url],lambda ref,cand,out:sync_and_verify(ref,cand,out),"Sync by Vikky.mkv","🎛️ Waveform Sync / Dynamic Drift Correction...")

@app.function(image=base_image,volumes={str(DATA_DIR):media_volume},secrets=[telegram_secret,remote_secret],cpu=8,memory=32768,timeout=7200,max_containers=1)
def process_encode_task(job_id:str,chat_id:int,source_url:str):
    from app.encode import encode_to_mkv
    execute_media_job(job_id,chat_id,"x265 Encode (3-5 GiB)",[source_url],lambda source,out:encode_to_mkv(source,out,target_min_gb=3.0,target_max_gb=5.0,max_attempts=3),"Vikky x265 Encode.mkv","⚙️ Original x265 Target-Size Encoding...")

@app.function(image=base_image,volumes={str(DATA_DIR):media_volume},secrets=[telegram_secret,remote_secret],cpu=8,memory=32768,timeout=7200,max_containers=1)
def process_remaster_task(job_id:str,chat_id:int,source_url:str,four_k:bool=False):
    name="4K Theater Remaster" if four_k else "1080p Hybrid Remaster"; filename="Vikky 4K Theater Remaster.mkv" if four_k else "Vikky Hybrid Remaster 1080p.mkv"
    execute_media_job(job_id,chat_id,name,[source_url],lambda source,out:run_ffmpeg_remaster(job_id,source,out,four_k),filename,"🎬 4K Theater Master / 30+ Filter Equivalent..." if four_k else "🎬 15+ Filter Hybrid Remaster...")

def submit_job(chat_id:int,kind:str,args:list[str])->tuple[str,int]:
    job_id=os.urandom(4).hex(); enqueue_job({"job_id":job_id,"chat_id":chat_id,"kind":kind,"args":args,"created_at":time.time()})
    try: dispatch_next()
    except Exception as exc: print(f"Initial queue dispatch error: {exc}",file=sys.stderr)
    active=get_active_job(); return job_id,(0 if active and active.get("job_id")==job_id else queue_position(job_id))


web_app = FastAPI(title="Vikky Movie AI Bot Control Plane")


@web_app.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": APP_NAME,
    }


@web_app.post("/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    try: data=await request.json()
    except Exception: return JSONResponse(status_code=200,content={"status":"ignored"})
    callback=data.get("callback_query")
    if callback:
        cid=str(callback.get("id") or ""); payload=str(callback.get("data") or ""); msg=callback.get("message") or {}; chat_id=int((msg.get("chat") or {}).get("id") or 0)
        if payload.startswith("abort_"):
            job_id=payload[6:]; active=get_active_job()
            if active and active.get("job_id")==job_id:
                request_abort(job_id); cleanup_scratch(JOBS_DIR/job_id/"scratch"); answer_callback(cid,"Cancellation requested"); edit_tg_message(chat_id,msg.get("message_id"),"🛑 Process Cancelled by User. Scratch disk scrubbed.",{"inline_keyboard":[]})
            else: answer_callback(cid,"Job is not active")
        else: answer_callback(cid)
        return JSONResponse(status_code=200,content={"status":"ok"})
    msg=data.get("message") or data.get("edited_message")
    if not msg or "text" not in msg:return JSONResponse(status_code=200,content={"status":"ignored"})
    chat_id=int((msg.get("chat") or {}).get("id") or 0); text=str(msg.get("text") or "").strip()
    if text in {"/start","/help"}:
        clear_user_state(chat_id); send_tg_message(chat_id,"🤖 Vikky Movie AI Bot\n\nSelect a media operation:",menu=True); return JSONResponse(status_code=200,content={"status":"ok"})
    if text in {"❌ Cancel / Reset","/cancel"}:
        state=get_user_state(chat_id); active=get_active_job()
        if state and state.get("job_id"):request_abort(str(state["job_id"]))
        if active and int(active.get("chat_id",-1))==chat_id:request_abort(str(active["job_id"]))
        clear_user_state(chat_id); send_tg_message(chat_id,"🛑 Cancel / Reset requested. Conversation state cleared.",menu=True); return JSONResponse(status_code=200,content={"status":"ok"})
    if text=="📊 Cluster Status":
        active=get_active_job(); pending=len(load_queue()); send_tg_message(chat_id,"🟢 Cluster Status: Online\n\nActive: "+str(active.get("job_id") if active else "None")+"\nPending FIFO: "+str(pending)+"\nWorkers: 8 CPU / 32 GB RAM / 7200s\nGPU: disabled\nStorage: /data\nConcurrency: 1",menu=True); return JSONResponse(status_code=200,content={"status":"ok"})
    prompts={"🔄 Audio Sync":("sync","📥 Step 1/2: Please send the Main Video Source (Candidate Video) direct link:"),"📦 x265 Encode (3-5 GiB)":("encode_x265","📥 Please send the Video direct link for High-Efficiency x265 Encoding (Target: 3-5 GiB):"),"🎬 1080p Hybrid Remaster":("remaster_1080p","📥 Please send the Video direct link to Remaster & Encode (1080p x265 15+ Filters):"),"👑 4K Theater Remaster":("remaster_4k","📥 Please send the Video direct link for 4K Theater Remastering (3840x2160 30+ Filters):")}
    if text in prompts:
        action,prompt=prompts[text]; set_user_state(chat_id,{"action":action,"step":"awaiting_video"}); send_tg_message(chat_id,prompt,menu=True); return JSONResponse(status_code=200,content={"status":"awaiting_video"})
    state=get_user_state(chat_id)
    if state:
        if not is_valid_url(text): send_tg_message(chat_id,"❌ Please provide a valid HTTP/HTTPS direct media link.",menu=True); return JSONResponse(status_code=200,content={"status":"invalid_url"})
        action,step=state.get("action"),state.get("step")
        if action=="sync" and step=="awaiting_video":
            set_user_state(chat_id,{"action":"sync","step":"awaiting_audio","candidate_url":text}); send_tg_message(chat_id,"🎵 Step 2/2: Now send the Audio Source direct link (or a Reference Video containing the audio):",menu=True); return JSONResponse(status_code=200,content={"status":"awaiting_audio"})
        clear_user_state(chat_id)
        if action=="sync" and step=="awaiting_audio": job_id,pos=submit_job(chat_id,"sync",[str(state["candidate_url"]),text])
        elif action=="encode_x265" and step=="awaiting_video": job_id,pos=submit_job(chat_id,"encode_x265",[text])
        elif action=="remaster_1080p" and step=="awaiting_video": job_id,pos=submit_job(chat_id,"remaster_1080p",[text])
        elif action=="remaster_4k" and step=="awaiting_video": job_id,pos=submit_job(chat_id,"remaster_4k",[text])
        else: send_tg_message(chat_id,"❌ Invalid conversation state. Please start again.",menu=True); return JSONResponse(status_code=200,content={"status":"invalid_state"})
        if pos: send_tg_message(chat_id,"⏳ Task Added to Queue (Position: #"+str(pos)+")\nJob ID: "+job_id+"\nProcessing will automatically begin as soon as the active job completes.",menu=True)
        return JSONResponse(status_code=200,content={"status":"queued","job_id":job_id})
    send_tg_message(chat_id,"Please choose an operation from the menu below.",menu=True); return JSONResponse(status_code=200,content={"status":"ignored"})


@web_app.get("/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    authorization = request.headers.get("Authorization", "")
    expected_token = os.environ.get("VIKKY_REMOTE_TOKEN", "")

    if not expected_token or authorization != f"Bearer {expected_token}":
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized"},
        )

    jobs_data: list[dict[str, Any]] = []

    if JOBS_DIR.exists():
        for path in sorted(JOBS_DIR.iterdir(), key=lambda p: p.name):
            if not path.is_dir():
                continue

            jobs_data.append(
                {
                    "job_id": path.name,
                    "retained_files": sorted(
                        file.name
                        for file in path.iterdir()
                        if file.is_file()
                    ),
                }
            )

    return JSONResponse(
        content={
            "status": "ok",
            "jobs": jobs_data,
        }
    )


@app.function(
    image=base_image,
    secrets=[telegram_secret, remote_secret],
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
)
@modal.asgi_app()
def api():
    return web_app

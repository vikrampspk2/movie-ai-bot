import asyncio
import logging
import re
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from .backends import backend_selector
from .config import settings
from .models import Job, JobType
from .queue import queue
from .worker import worker_loop
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vikky-bot")
api = FastAPI(title="Vikky Movie AI Bot", version="0.1.0")
MEDIA_RE = re.compile(r"\.(mkv|mp4|m4v|mov|webm|avi|ts|m2ts|zip|iso)$", re.I)
@api.get("/health")
async def health(): return {"status":"ok","service":"vikky-movie-ai-bot"}
def _workspace(user_id:int)->Path:
    path=settings.workspace_root/str(user_id)/uuid4().hex; path.mkdir(parents=True,exist_ok=True); return path
def _job_type(context): 
    value=context.user_data.get("pending_job_type"); return JobType(value) if value else None
async def start(update,context): await update.message.reply_text("🎬 Vikky Movie AI Bot\n\n/sync — sync two media files\n/upscale — real AI 4K\n/encode — MKV encode\n/status — live status\n/queue — queue\n/cancel <job_id> — cancel\n/help — commands")
async def help_command(update,context): await update.message.reply_text("/sync, /upscale, /encode → choose a job, then send media\n/status → live job\n/queue → queue\n/cancel <job_id> → cancel")
async def choose_job(update,context):
    job_type=JobType(update.message.text.split()[0].lstrip("/").lower()); context.user_data["pending_job_type"]=job_type.value; context.user_data["sync_files"]=[]
    await update.message.reply_text(f"✅ {job_type.value.upper()} selected. Send the media now.")
async def receive_media(update,context):
    job_type=_job_type(context)
    if not job_type or not update.message:return
    media=update.message.document or update.message.video
    if media is None:return
    name=Path(getattr(media,"file_name",None) or f"telegram_{uuid4().hex}.bin").name
    if not MEDIA_RE.search(name): await update.message.reply_text("❌ Unsupported media type."); return
    workspace=_workspace(update.effective_user.id); destination=workspace/name
    tg_file=await context.bot.get_file(media.file_id); await tg_file.download_to_drive(custom_path=destination)
    if job_type==JobType.SYNC:
        files=context.user_data.setdefault("sync_files",[]); files.append(destination)
        if len(files)==1: await update.message.reply_text("📌 Send the second media file for reference/candidate sync."); return
        job=Job(type=job_type,source_name=files[1].name,source_path=files[1],reference_path=files[0],owner_id=update.effective_user.id); context.user_data.pop("pending_job_type",None); context.user_data.pop("sync_files",None)
    else:
        job=Job(type=job_type,source_name=name,source_path=destination,owner_id=update.effective_user.id); context.user_data.pop("pending_job_type",None)
    await queue.add(job); jobs=await queue.snapshot(); pos=sum(1 for x in jobs if x.status.value=="queued" and x.id!=job.id)+1
    await update.message.reply_text(f"📥 Accepted: {name}\n🆔 Job: {job.id}\n📋 Queue position: {pos}\n⚡ Status: queued")
async def status(update,context):
    jobs=await queue.snapshot(); running=next((j for j in jobs if j.status.value=="running"),None); selected=backend_selector.select(running) if running else None
    text=f"🟢 Vikky status\nJobs: {len(jobs)}\nCurrent: {running.id if running else 'idle'}\nBackend: {selected.name if selected else 'not configured'}"
    if running:text+=f"\nStage: {running.stage}\nProgress: {running.progress:.0f}%"
    await update.message.reply_text(text)
async def queue_command(update,context):
    jobs=await queue.snapshot()
    await update.message.reply_text("📭 Queue empty" if not jobs else "📋 Queue\n"+"\n".join(f"{j.id} · {j.type.value} · {j.status.value} · {j.progress:.0f}%" for j in jobs[-10:]))
async def cancel(update,context):
    if not context.args: await update.message.reply_text("Usage: /cancel <job_id>"); return
    await update.message.reply_text("✅ Cancelled" if await queue.cancel(context.args[0]) else "❌ Job not found or already finished")
async def run():
    if not settings.telegram_bot_token: raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    settings.workspace_root.mkdir(parents=True,exist_ok=True)
    application=Application.builder().token(settings.telegram_bot_token).build()
    for command,handler in [("start",start),("help",help_command),("sync",choose_job),("upscale",choose_job),("encode",choose_job),("status",status),("queue",queue_command),("cancel",cancel)]: application.add_handler(CommandHandler(command,handler))
    application.add_handler(MessageHandler(filters.Document.ALL|filters.VIDEO,receive_media))
    await application.initialize(); asyncio.create_task(worker_loop(application.bot)); await application.start(); await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try: await asyncio.Event().wait()
    finally: await application.updater.stop(); await application.stop(); await application.shutdown()
if __name__=="__main__": asyncio.run(run())

"""
Daemon Mode - Independent WebUI based on FastAPI, port 8765.

Provides a dark-themed dashboard for managing daemon triggers, memory,
tasks, and a chat interface for interacting with the daemon core.

Runs independently of the main AutoRUN WebUI.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

# ── Embedded HTML (dark theme, modern design) ─────────────────────────────────────

DAEMON_WEBUI_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoRUN Daemon Mode</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;background:#0d1117;color:#c9d1d9;display:flex;height:100vh}
.sidebar{width:220px;background:#161b22;border-right:1px solid #30363d;padding:16px;display:flex;flex-direction:column}
.sidebar h2{font-size:16px;color:#58a6ff;margin-bottom:20px}
.nav-item{padding:8px 12px;margin:2px 0;border-radius:6px;cursor:pointer;font-size:13px;color:#8b949e}
.nav-item:hover,.nav-item.active{background:#1c2128;color:#e6edf3}
.main{flex:1;display:flex;flex-direction:column}
.header{padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;font-size:14px}
.content{flex:1;padding:20px;overflow-y:auto}
.panel{display:none}
.panel.active{display:block}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:12px}
.card h3{font-size:14px;color:#58a6ff;margin-bottom:10px}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center}
.stat-value{font-size:28px;font-weight:bold;color:#58a6ff}
.stat-label{font-size:11px;color:#8b949e;margin-top:4px}
.btn{padding:6px 14px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer;font-size:12px}
.btn:hover{background:#30363d}
.btn-primary{background:#238636;border-color:#2ea043;color:white}
.btn-danger{background:#da3633;border-color:#f85149;color:white}
input,select,textarea{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:6px 10px;border-radius:6px;font-size:12px;width:100%}
input:focus,select:focus{outline:none;border-color:#58a6ff}
.chat-box{height:400px;overflow-y:auto;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:10px}
.chat-msg{margin:6px 0;padding:8px 12px;border-radius:6px;font-size:12px;max-width:80%;word-wrap:break-word}
.chat-msg.user{background:#1f6feb;color:white;margin-left:auto}
.chat-msg.system{background:#21262d;color:#8b949e}
.chat-input{display:flex;gap:8px}
.chat-input input{flex:1}
.memory-list{font-size:12px}
.memory-list table{width:100%;border-collapse:collapse}
.memory-list td,.memory-list th{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left}
.memory-list th{color:#8b949e;font-weight:normal;font-size:11px}
.trigger-form{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.trigger-item{padding:8px 12px;border-bottom:1px solid #30363d;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.task-item{padding:8px 12px;border-bottom:1px solid #30363d;font-size:12px}
.badge{padding:2px 6px;border-radius:10px;font-size:10px}
.badge-running{background:#1f6feb33;color:#58a6ff}
.badge-done{background:#23863633;color:#3fb950}
.badge-error{background:#da363333;color:#f85149}
.badge-pending{background:#d2992233;color:#d29922}
.event-item{padding:6px 12px;border-bottom:1px solid #21262d;font-size:12px;color:#8b949e}
</style>
</head>
<body>
<div class="sidebar">
  <h2>Daemon Mode</h2>
  <div class="nav-item active" data-panel="dashboard">Dashboard</div>
  <div class="nav-item" data-panel="triggers">Triggers</div>
  <div class="nav-item" data-panel="memory">Memory</div>
  <div class="nav-item" data-panel="tasks">Tasks</div>
  <div class="nav-item" data-panel="chat">Chat</div>
</div>
<div class="main">
  <div class="header" id="header">AutoRUN Daemon Mode v1.0</div>
  <div class="content" id="content">
    <div class="panel active" id="panel-dashboard">
      <div class="stat-grid" id="stats"></div>
      <div class="card"><h3>Recent Events</h3><div id="recent-events"></div></div>
    </div>
    <div class="panel" id="panel-triggers">
      <div class="card"><h3>Add Trigger</h3>
        <div class="trigger-form">
          <input id="trig-name" placeholder="Name">
          <select id="trig-type" onchange="onTrigTypeChange()">
            <option value="time">Time Trigger</option>
            <option value="alarm">Alarm Trigger</option>
          </select>
          <input id="trig-interval" placeholder="Interval (seconds)" value="1200">
          <select id="trig-alarm-type" style="display:none">
            <option value="once">Once</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
          <input id="trig-alarm-time" placeholder="HH:MM" value="09:00" style="display:none">
          <input id="trig-prompt" placeholder="Trigger prompt / description">
        </div>
        <button class="btn btn-primary" onclick="addTrigger()" style="margin-top:8px">Add</button>
        <div id="add-trigger-result" style="margin-top:8px;font-size:12px"></div>
      </div>
      <div class="card"><h3>Trigger List</h3><div id="trigger-list"></div></div>
    </div>
    <div class="panel" id="panel-memory">
      <div class="card"><h3>Short-term Memory</h3><div id="short-memory" class="memory-list"></div></div>
      <div class="card"><h3>Mid-term Memory</h3><div id="mid-memory" class="memory-list"></div></div>
      <div class="card"><h3>Long-term Memory</h3><div id="long-memory" class="memory-list"></div></div>
    </div>
    <div class="panel" id="panel-tasks">
      <div class="card"><h3>Task List</h3><div id="task-list"></div></div>
    </div>
    <div class="panel" id="panel-chat">
      <div class="card">
        <div class="chat-box" id="chat-box"><div class="chat-msg system">Daemon Mode WebUI connected.</div></div>
        <div class="chat-input">
          <input id="chat-input" placeholder="Enter message..." onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn btn-primary" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
var ws;
function connect(){
  ws=new WebSocket('ws://'+location.host+'/ws');
  ws.onmessage=function(e){
    try{
      var d=JSON.parse(e.data);
      if(d.type==='stats')updateStats(d.data);
      if(d.type==='triggers')updateTriggers(d.data);
      if(d.type==='memory')updateMemory(d.data);
      if(d.type==='tasks')updateTasks(d.data);
      if(d.type==='chat')addChatMsg('system',d.text);
      if(d.type==='events')updateEvents(d.data);
      if(d.type==='chat_reply')addChatMsg('system',d.text);
    }catch(ex){}
  };
  ws.onclose=function(){setTimeout(connect,3000)};
}
function updateStats(d){
  document.getElementById('stats').innerHTML=
    '<div class="stat-card"><div class="stat-value">'+d.api_calls+'</div><div class="stat-label">API Calls</div></div>'+
    '<div class="stat-card"><div class="stat-value">'+d.triggers+'</div><div class="stat-label">Trigger Fires</div></div>'+
    '<div class="stat-card"><div class="stat-value">'+d.tasks+'</div><div class="stat-label">Tasks</div></div>'+
    '<div class="stat-card"><div class="stat-value">'+d.uptime+'</div><div class="stat-label">Uptime</div></div>';
}
function updateTriggers(data){
  var list=document.getElementById('trigger-list');
  if(!data||data.length===0){list.innerHTML='<div class="event-item">No triggers configured.</div>';return;}
  var html='';
  data.forEach(function(t){
    var typeLabel=t.type||'unknown';
    if(t.type==='time')typeLabel='Time ('+(t.interval_seconds||'?')+'s)';
    else if(t.type==='alarm')typeLabel='Alarm ('+(t.trigger_type||'?')+')';
    html+='<div class="trigger-item">'+
      '<span><strong>'+esc(t.name)+'</strong> <span class="badge badge-pending">'+typeLabel+'</span></span>'+
      '<button class="btn btn-danger" onclick="deleteTrigger(\''+esc(t.id)+'\')">Delete</button>'+
    '</div>';
  });
  list.innerHTML=html;
}
function updateMemory(data){
  document.getElementById('short-memory').innerHTML='<table><tr><th>Metric</th><th>Value</th></tr>'+
    '<tr><td>Entries</td><td>'+data.short.count+'</td></tr>'+
    '<tr><td>Total Chars</td><td>'+data.short.chars+' / '+data.short.max_chars+'</td></tr>'+
    '<tr><td>Usage</td><td>'+data.short.usage_pct+'%</td></tr></table>';
  document.getElementById('mid-memory').innerHTML='<table><tr><th>Metric</th><th>Value</th></tr>'+
    '<tr><td>Entries</td><td>'+data.mid.count+'</td></tr>'+
    '<tr><td>Total Chars</td><td>'+data.mid.chars+' / '+data.mid.max_chars+'</td></tr>'+
    '<tr><td>Usage</td><td>'+data.mid.usage_pct+'%</td></tr></table>';
  document.getElementById('long-memory').innerHTML='<table><tr><th>Metric</th><th>Value</th></tr>'+
    '<tr><td>Entries</td><td>'+data.long.count+'</td></tr>'+
    '<tr><td>Total Chars</td><td>'+data.long.chars+' / '+data.long.max_chars+'</td></tr>'+
    '<tr><td>Usage</td><td>'+data.long.usage_pct+'%</td></tr></table>';
}
function updateTasks(data){
  var list=document.getElementById('task-list');
  if(!data||data.length===0){list.innerHTML='<div class="event-item">No tasks.</div>';return;}
  var html='';
  data.forEach(function(t){
    var badgeClass='badge-pending';
    if(t.state==='running')badgeClass='badge-running';
    else if(t.state==='completed')badgeClass='badge-done';
    else if(t.state==='crashed'||t.state==='timeout')badgeClass='badge-error';
    html+='<div class="task-item">'+
      '<span class="badge '+badgeClass+'">'+t.state+'</span> '+
      '<strong>'+esc(t.prompt?t.prompt.substring(0,80):t.id)+'</strong>'+
      '<span style="float:right;color:#8b949e">'+formatElapsed(t.elapsed)+'</span>'+
    '</div>';
  });
  list.innerHTML=html;
}
function updateEvents(data){
  var el=document.getElementById('recent-events');
  if(!data||data.length===0){el.innerHTML='<div class="event-item">No recent events.</div>';return;}
  var html='';
  data.forEach(function(ev){
    html+='<div class="event-item">['+ev.time+'] '+esc(ev.text)+'</div>';
  });
  el.innerHTML=html;
}
function addChatMsg(type,text){
  var d=document.getElementById('chat-box');
  d.innerHTML+='<div class="chat-msg '+type+'">'+esc(text)+'</div>';
  d.scrollTop=d.scrollHeight;
}
function sendChat(){
  var inp=document.getElementById('chat-input');
  var t=inp.value.trim();
  if(!t)return;
  addChatMsg('user',t);
  ws.send(JSON.stringify({type:'chat',text:t}));
  inp.value='';
}
function addTrigger(){
  var name=document.getElementById('trig-name').value.trim();
  var type=document.getElementById('trig-type').value;
  var interval=parseInt(document.getElementById('trig-interval').value)||1200;
  var alarmType=document.getElementById('trig-alarm-type').value;
  var alarmTime=document.getElementById('trig-alarm-time').value.trim();
  var prompt=document.getElementById('trig-prompt').value.trim();
  if(!name){alert('Please enter a name.');return;}
  ws.send(JSON.stringify({
    type:'add_trigger',
    trig_type:type,
    name:name,
    interval:interval,
    alarm_type:alarmType,
    alarm_time:alarmTime,
    prompt:prompt
  }));
  document.getElementById('add-trigger-result').innerHTML='<span style="color:#3fb950">Trigger added.</span>';
  setTimeout(function(){document.getElementById('add-trigger-result').innerHTML='';},3000);
}
function deleteTrigger(id){
  ws.send(JSON.stringify({type:'delete_trigger',id:id}));
}
function onTrigTypeChange(){
  var type=document.getElementById('trig-type').value;
  document.getElementById('trig-interval').style.display=(type==='time')?'':'none';
  document.getElementById('trig-alarm-type').style.display=(type==='alarm')?'':'none';
  document.getElementById('trig-alarm-time').style.display=(type==='alarm')?'':'none';
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function formatElapsed(secs){
  if(!secs||secs<=0)return '';
  var m=Math.floor(secs/60);
  var s=Math.floor(secs%60);
  if(m>0)return m+'m '+s+'s';
  return s+'s';
}
// Navigation
document.querySelectorAll('.nav-item').forEach(function(el){
  el.onclick=function(){
    document.querySelectorAll('.nav-item').forEach(function(e){e.classList.remove('active')});
    el.classList.add('active');
    document.querySelectorAll('.panel').forEach(function(p){p.classList.remove('active')});
    document.getElementById('panel-'+el.dataset.panel).classList.add('active');
    ws.send(JSON.stringify({type:'refresh',panel:el.dataset.panel}));
  };
});
connect();
setInterval(function(){
  if(ws&&ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'refresh'}));
},5000);
</script>
</body>
</html>"""


# ── DaemonWebUI Class ─────────────────────────────────────────────────────────────

class DaemonWebUI:
    """Daemon Mode independent WebUI.

    Communicates with DaemonCore via direct reference (in-process) or
    through the singleton accessor. Serves a dark-themed SPA dashboard
    with real-time WebSocket updates.

    Usage:
        ui = DaemonWebUI(daemon_core)
        await ui.start()      # async (asyncio event loop)
        # or
        ui.run_sync()         # blocking (convenience)
    """

    def __init__(self, daemon_core=None, host: str = "127.0.0.1", port: int = 8765):
        self._core = daemon_core
        self._host = host
        self._port = port
        self._app = FastAPI(title="AutoRUN Daemon WebUI")
        self._ws_clients: List[WebSocket] = []
        self._setup_routes()

    @property
    def core(self):
        """Lazy-load the DaemonCore singleton if not provided."""
        if self._core is None:
            try:
                from .daemon_core import get_daemon_core
                self._core = get_daemon_core()
            except Exception:
                pass
        return self._core

    def _setup_routes(self) -> None:
        app = self._app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return DAEMON_WEBUI_HTML

        @app.get("/api/status")
        async def get_status():
            core = self.core
            if not core:
                return {"state": "no_core", "error": "DaemonCore not initialized"}
            status = core.get_status()
            uptime = int(status.get("uptime", 0))
            return {
                "state": status.get("state", "unknown"),
                "session_id": status.get("session_id", ""),
                "api_calls": status.get("api_call_count", 0),
                "triggers": status.get("trigger_count", 0),
                "tasks": status.get("task_count", 0),
                "active_tasks": status.get("active_tasks", 0),
                "uptime": f"{uptime//3600}h {(uptime%3600)//60}m {uptime%60}s",
                "memory_stats": status.get("memory_stats", {}),
                "trigger_count_total": status.get("trigger_count_total", 0),
            }

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            self._ws_clients.append(websocket)
            try:
                await self._send_stats(websocket)
                while True:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    await self._handle_ws_message(websocket, msg)
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.warning("WebSocket error", exc_info=True)
            finally:
                if websocket in self._ws_clients:
                    self._ws_clients.remove(websocket)

        @app.post("/api/trigger")
        async def add_trigger_api(request: Request):
            body = await request.json()
            core = self.core
            if not core:
                return JSONResponse({"ok": False, "error": "Core not available"}, status_code=503)

            ttype = body.get("trig_type", body.get("type", "time"))
            name = body.get("name", "manual")
            prompt = body.get("prompt", "")

            try:
                if ttype == "alarm":
                    alarm_time = body.get("alarm_time", "09:00")
                    alarm_type = body.get("alarm_type", "daily")
                    if alarm_type == "once":
                        core.triggers.add_alarm_trigger(
                            name, trigger_type="once",
                            daily_time=alarm_time,
                            metadata={"prompt": prompt},
                        )
                    elif alarm_type == "weekly":
                        core.triggers.add_alarm_trigger(
                            name, trigger_type="weekly",
                            weekly_time=alarm_time,
                            metadata={"prompt": prompt},
                        )
                    else:
                        core.triggers.add_alarm_trigger(
                            name, trigger_type="daily",
                            daily_time=alarm_time,
                            metadata={"prompt": prompt},
                        )
                else:
                    interval = int(body.get("interval", 1200))
                    core.triggers.add_time_trigger(
                        name, interval_seconds=interval,
                        metadata={"prompt": prompt},
                    )
                await self._broadcast_full_state()
                return {"ok": True}
            except Exception as e:
                logger.error("Failed to add trigger: %s", e)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        @app.delete("/api/trigger/{trigger_id}")
        async def delete_trigger_api(trigger_id: str):
            core = self.core
            if not core:
                return JSONResponse({"ok": False, "error": "Core not available"}, status_code=503)
            deleted = core.triggers.remove_trigger(trigger_id)
            if deleted:
                await self._broadcast_full_state()
            return {"ok": deleted}

        @app.get("/api/memory")
        async def get_memory_api():
            core = self.core
            if not core:
                return {"short": {}, "mid": {}, "long": {}}
            stats = core.memory.get_stats()
            return _build_memory_response(stats)

        @app.get("/api/tasks")
        async def get_tasks_api():
            core = self.core
            if not core:
                return {"tasks": []}
            return {"tasks": [t.to_dict() for t in core.get_all_tasks()]}

        @app.get("/api/triggers")
        async def get_triggers_api():
            core = self.core
            if not core:
                return {"triggers": []}
            return {"triggers": core.triggers.get_all_triggers()}

    # ── WebSocket Message Handling ─────────────────────────────────────────────────

    async def _handle_ws_message(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "chat":
            text = msg.get("text", "")
            core = self.core
            if core and hasattr(core, "submit_task"):
                # Submit the user message as a task to the daemon core
                task = await core.submit_task(text, metadata={"source": "webui_chat"})
                await self._broadcast({
                    "type": "chat_reply",
                    "text": f"Task submitted: {task.id} - will execute shortly.",
                })
            else:
                await self._broadcast({
                    "type": "chat_reply",
                    "text": f"Received: {text[:200]} (DaemonCore not available for execution)",
                })

        elif msg_type == "refresh":
            panel = msg.get("panel", "")
            await self._send_full_state(ws, panel)

        elif msg_type == "add_trigger":
            await self._handle_ws_add_trigger(ws, msg)

        elif msg_type == "delete_trigger":
            trigger_id = msg.get("id", "")
            core = self.core
            if core:
                core.triggers.remove_trigger(trigger_id)
            await self._broadcast_full_state()

    async def _handle_ws_add_trigger(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        core = self.core
        if not core:
            await ws.send_json({"type": "chat", "text": "Error: DaemonCore not available"})
            return

        ttype = msg.get("trig_type", "time")
        name = msg.get("name", "manual")
        prompt = msg.get("prompt", "")

        try:
            if ttype == "alarm":
                alarm_time = msg.get("alarm_time", "09:00")
                alarm_type = msg.get("alarm_type", "daily")
                if alarm_type == "once":
                    core.triggers.add_alarm_trigger(
                        name, trigger_type="once",
                        daily_time=alarm_time,
                        metadata={"prompt": prompt},
                    )
                elif alarm_type == "weekly":
                    core.triggers.add_alarm_trigger(
                        name, trigger_type="weekly",
                        weekly_time=alarm_time,
                        metadata={"prompt": prompt},
                    )
                else:
                    core.triggers.add_alarm_trigger(
                        name, trigger_type="daily",
                        daily_time=alarm_time,
                        metadata={"prompt": prompt},
                    )
            else:
                interval = int(msg.get("interval", 1200))
                core.triggers.add_time_trigger(
                    name, interval_seconds=interval,
                    metadata={"prompt": prompt},
                )
        except Exception as e:
            await ws.send_json({"type": "chat", "text": f"Error adding trigger: {e}"})

        await self._broadcast_full_state()

    # ── Broadcasting ───────────────────────────────────────────────────────────────

    async def _send_stats(self, ws: WebSocket) -> None:
        """Send core statistics to a single WebSocket client."""
        core = self.core
        uptime = 0
        if core and core._started_at > 0:
            uptime = int(time.time() - core._started_at)
        await ws.send_json({
            "type": "stats",
            "data": {
                "api_calls": core._api_call_count if core else 0,
                "triggers": core._trigger_count if core else 0,
                "tasks": core._task_count if core else 0,
                "uptime": f"{uptime//3600}h {(uptime%3600)//60}m {uptime%60}s",
            },
        })

    async def _send_full_state(self, ws: WebSocket, panel: str = "") -> None:
        """Send full state for a panel (or all panels) to a single WebSocket."""
        core = self.core
        if not core:
            await ws.send_json({"type": "chat", "text": "DaemonCore not initialized"})
            return

        # Always send stats
        await self._send_stats(ws)

        # Send panel-specific data based on request or current panel
        if panel == "triggers" or not panel:
            triggers_data = core.triggers.get_all_triggers()
            await ws.send_json({"type": "triggers", "data": triggers_data})

        if panel == "memory" or not panel:
            stats = core.memory.get_stats()
            await ws.send_json({"type": "memory", "data": _build_memory_response(stats)})

        if panel == "tasks" or not panel:
            tasks_data = [t.to_dict() for t in core.get_all_tasks()]
            await ws.send_json({"type": "tasks", "data": tasks_data})

        if panel == "dashboard" or not panel:
            # Recent events from short-term memory
            events = _build_recent_events(core)
            await ws.send_json({"type": "events", "data": events})

    async def _broadcast_stats(self) -> None:
        """Broadcast stats to all connected WebSocket clients."""
        for ws in list(self._ws_clients):
            try:
                await self._send_stats(ws)
            except Exception:
                pass

    async def _broadcast_full_state(self) -> None:
        """Broadcast full state to all connected WebSocket clients."""
        for ws in list(self._ws_clients):
            try:
                await self._send_full_state(ws)
            except Exception:
                pass

    async def _broadcast(self, msg: Dict[str, Any]) -> None:
        """Broadcast a generic message to all connected WebSocket clients."""
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    # ── Lifecycle ──────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the WebUI server (asyncio)."""
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        logger.info("Daemon WebUI starting on http://%s:%s", self._host, self._port)
        await server.serve()

    def run_sync(self) -> None:
        """Start the WebUI server (blocking, for standalone use)."""
        logger.info("Daemon WebUI starting on http://%s:%s", self._host, self._port)
        uvicorn.run(self._app, host=self._host, port=self._port, log_level="info")


# ── Helper Functions ──────────────────────────────────────────────────────────────

def _build_memory_response(stats: Dict[str, Any]) -> Dict[str, Any]:
    """Build a structured memory response from raw stats."""
    short_count = stats.get("short_term_count", 0)
    short_chars = stats.get("short_term_chars", 0)
    short_max = stats.get("short_term_max_chars", 15000)
    mid_count = stats.get("mid_term_count", 0)
    mid_chars = stats.get("mid_term_chars", 0)
    mid_max = stats.get("mid_term_max_chars", 20000)
    long_count = stats.get("long_term_count", 0)
    long_chars = stats.get("long_term_chars", 0)
    long_max = stats.get("long_term_max_chars", 10000)

    return {
        "short": {
            "count": short_count,
            "chars": short_chars,
            "max_chars": short_max,
            "usage_pct": round(short_chars / short_max * 100, 1) if short_max > 0 else 0,
        },
        "mid": {
            "count": mid_count,
            "chars": mid_chars,
            "max_chars": mid_max,
            "usage_pct": round(mid_chars / mid_max * 100, 1) if mid_max > 0 else 0,
        },
        "long": {
            "count": long_count,
            "chars": long_chars,
            "max_chars": long_max,
            "usage_pct": round(long_chars / long_max * 100, 1) if long_max > 0 else 0,
        },
    }


def _build_recent_events(core) -> List[Dict[str, str]]:
    """Build a list of recent events from short-term memory."""
    events = []
    try:
        # Access short-term memory entries (last 10)
        entries = getattr(core.memory, "_short_term", [])
        for entry in entries[-10:]:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
            text = entry.content[:120] if hasattr(entry, "content") else str(entry)[:120]
            events.append({"time": ts, "text": text})
    except Exception:
        pass
    return events


# ── Standalone Entry Point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ui = DaemonWebUI()
    ui.run_sync()

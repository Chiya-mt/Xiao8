# -*- coding: utf-8 -*-
import asyncio
import json
import traceback
import sys
import uuid
import logging
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, File, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from main_helper import core as core, cross_server as cross_server
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from utils.preferences import load_user_preferences, update_model_preferences, validate_model_preferences, move_model_to_top
from utils.frontend_utils import find_models, load_characters, save_characters
from multiprocessing import Process, Queue, Event
import os
import atexit
import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService
import requests
templates = Jinja2Templates(directory="./")
from config import get_character_data, MAIN_SERVER_PORT, CORE_API_KEY

# Configure logging
def setup_logging():
    """Setup logging configuration"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f'lanlan_server_{datetime.now().strftime("%Y%m%d")}.log', encoding='utf-8')
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

def cleanup():
    logger.info("Starting cleanup process")
    for k in sync_message_queue:
        while sync_message_queue[k] and not sync_message_queue[k].empty():
            sync_message_queue[k].get_nowait()
        sync_message_queue[k].close()
        sync_message_queue[k].join_thread()
    logger.info("Cleanup completed")
atexit.register(cleanup)
sync_message_queue = {}
sync_shutdown_event = {}
session_manager = {}
session_id = {}
sync_process = {}
# Unpack character data once for initialization
master_name, her_name, master_basic_config, lanlan_basic_config, name_mapping, lanlan_prompt, semantic_store, time_store, setting_store, recent_log = get_character_data()
catgirl_names = list(lanlan_prompt.keys())
for k in catgirl_names:
    sync_message_queue[k] = Queue()
    sync_shutdown_event[k] = Event()
    session_manager[k] = core.LLMSessionManager(
        sync_message_queue[k],
        k,
        lanlan_prompt[k].replace('{LANLAN_NAME}', k).replace('{MASTER_NAME}', master_name)
    )
    session_id[k] = None
    sync_process[k] = None
lock = asyncio.Lock()

# --- FastAPI App Setup ---
app = FastAPI()

# *** CORRECTED STATIC FILE MOUNTING ***
# Mount the 'static' directory under the URL path '/static'
# When a request comes in for /static/app.js, FastAPI will look for the file 'static/app.js'
# relative to where the server is running (gemini-live-app/).
app.mount("/static", StaticFiles(directory="static"), name="static")


# *** CORRECTED ROOT PATH TO SERVE index.html ***
@app.get("/", response_class=HTMLResponse)
async def get_default_index(request: Request):
    # 每次动态获取角色数据
    _, her_name, _, lanlan_basic_config, _, _, _, _, _, _ = get_character_data()
    # 获取live2d字段
    live2d = lanlan_basic_config.get(her_name, {}).get('live2d', 'mao_pro')
    # 查找所有模型
    models = find_models()
    # 根据live2d字段查找对应的model path
    model_path = next((m["path"] for m in models if m["name"] == live2d), f"/static/{live2d}/{live2d}.model3.json")
    return templates.TemplateResponse("templates/index.html", {
        "request": request,
        "lanlan_name": her_name,
        "model_path": model_path
    })

@app.get("/api/preferences")
async def get_preferences():
    """获取用户偏好设置"""
    preferences = load_user_preferences()
    return preferences

@app.post("/api/preferences")
async def save_preferences(request: Request):
    """保存用户偏好设置"""
    try:
        data = await request.json()
        if not data:
            return {"success": False, "error": "无效的数据"}
        
        # 验证偏好数据
        if not validate_model_preferences(data):
            return {"success": False, "error": "偏好数据格式无效"}
        
        # 更新偏好
        if update_model_preferences(data['model_path'], data['position'], data['scale']):
            return {"success": True, "message": "偏好设置已保存"}
        else:
            return {"success": False, "error": "保存失败"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/models")
async def get_models():
    """
    API接口，调用扫描函数并以JSON格式返回找到的模型列表。
    """
    models = find_models()
    return models

@app.post("/api/preferences/set-preferred")
async def set_preferred_model(request: Request):
    """设置首选模型"""
    try:
        data = await request.json()
        if not data or 'model_path' not in data:
            return {"success": False, "error": "无效的数据"}
        
        if move_model_to_top(data['model_path']):
            return {"success": True, "message": "首选模型已更新"}
        else:
            return {"success": False, "error": "模型不存在或更新失败"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.on_event("startup")
async def startup_event():
    global sync_process
    logger.info("Starting sync connector processes")
    # 启动同步连接器进程
    for k in sync_process:
        if sync_process[k] is None:
            sync_process[k] = Process(
                target=cross_server.sync_connector_process,
                args=(sync_message_queue[k], sync_shutdown_event[k], k, "ws://localhost:8002", {'bullet': False, 'monitor': False})
            )
            sync_process[k].start()
            logger.info(f"同步连接器进程已启动 (PID: {sync_process[k].pid})")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时执行"""
    logger.info("Shutting down sync connector processes")
    # 关闭同步服务器连接
    for k in sync_process:
        if sync_process[k] is not None:
            sync_shutdown_event[k].set()
            sync_process[k].join(timeout=3)  # 等待进程正常结束
            if sync_process[k].is_alive():
                sync_process[k].terminate()  # 如果超时，强制终止
    logger.info("同步连接器进程已停止")


@app.websocket("/ws/{lanlan_name}")
async def websocket_endpoint(websocket: WebSocket, lanlan_name: str):
    await websocket.accept()
    this_session_id = uuid.uuid4()
    async with lock:
        global session_id
        session_id[lanlan_name] = this_session_id
    logger.info(f"⭐websocketWebSocket accepted: {websocket.client}, new session id: {session_id[lanlan_name]}, lanlan_name: {lanlan_name}")

    try:
        while True:
            data = await websocket.receive_text()
            if session_id[lanlan_name] != this_session_id:
                await session_manager[lanlan_name].send_status(f"切换至另一个终端...")
                await websocket.close()
                break
            message = json.loads(data)
            action = message.get("action")
            # logger.debug(f"WebSocket received action: {action}") # Optional debug log

            if action == "start_session":
                session_manager[lanlan_name].active_session_is_idle = False
                input_type = message.get("input_type")
                if input_type in ['audio', 'screen', 'camera']:
                    asyncio.create_task(session_manager[lanlan_name].start_session(websocket, message.get("new_session", False)))
                else:
                    await session_manager[lanlan_name].send_status(f"Invalid input type: {input_type}")

            elif action == "stream_data":
                asyncio.create_task(session_manager[lanlan_name].stream_data(message))

            elif action == "end_session":
                session_manager[lanlan_name].active_session_is_idle = False
                asyncio.create_task(session_manager[lanlan_name].end_session())

            elif action == "pause_session":
                session_manager[lanlan_name].active_session_is_idle = True

            else:
                logger.warning(f"Unknown action received: {action}")
                await session_manager[lanlan_name].send_status(f"Unknown action: {action}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client}")
    except Exception as e:
        error_message = f"WebSocket handler error: {e}"
        logger.error(f"💥 {error_message}")
        logger.error(traceback.format_exc())
        try:
            await session_manager[lanlan_name].send_status(f"Server error: {e}")
        except:
            pass
    finally:
        logger.info(f"Cleaning up WebSocket resources: {websocket.client}")
        await session_manager[lanlan_name].cleanup()

@app.get("/l2d", response_class=HTMLResponse)
async def get_l2d_manager(request: Request, lanlan_name: str = ""):
    """渲染Live2D模型管理器页面"""
    return templates.TemplateResponse("templates/l2d_manager.html", {
        "request": request,
        "lanlan_name": lanlan_name
    })

@app.get('/chara_manager', response_class=HTMLResponse)
async def chara_manager(request: Request):
    """渲染主控制页面"""
    return templates.TemplateResponse('templates/chara_manager.html', {"request": request})

@app.get('/voice_clone', response_class=HTMLResponse)
async def voice_clone_page(request: Request, lanlan_name: str = ""):
    return templates.TemplateResponse("templates/voice_clone.html", {"request": request, "lanlan_name": lanlan_name})


@app.get('/api/characters')
async def get_characters():
    return JSONResponse(content=load_characters())

@app.post('/api/characters/master')
async def update_master(request: Request):
    data = await request.json()
    if not data or not data.get('档案名'):
        return JSONResponse({'success': False, 'error': '档案名为必填项'}, status_code=400)
    characters = load_characters()
    characters['主人'] = {k: v for k, v in data.items() if v}
    save_characters(characters)
    return {"success": True}

@app.post('/api/characters/catgirl')
async def add_catgirl(request: Request):
    data = await request.json()
    if not data or not data.get('档案名'):
        return JSONResponse({'success': False, 'error': '档案名为必填项'}, status_code=400)
    for field in ['live2d', 'voice_id', 'system_prompt']:
        if not data.get(field):
            return JSONResponse({'success': False, 'error': f'{field}为必填项'}, status_code=400)
    characters = load_characters()
    key = data['档案名']
    if key in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '该猫娘已存在'}, status_code=400)
    if '猫娘' not in characters:
        characters['猫娘'] = {}
    characters['猫娘'][key] = {k: v for k, v in data.items() if k != '档案名' and v}
    save_characters(characters)
    return {"success": True}

@app.put('/api/characters/catgirl/{name}')
async def update_catgirl(name: str, request: Request):
    data = await request.json()
    if not data:
        return JSONResponse({'success': False, 'error': '无数据'}, status_code=400)
    characters = load_characters()
    if name not in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '猫娘不存在'}, status_code=404)
    characters['猫娘'][name] = {k: v for k, v in data.items() if k != '档案名' and v}
    save_characters(characters)
    return {"success": True}

@app.put('/api/characters/catgirl/l2d/{name}')
async def update_catgirl_l2d(name: str, request: Request):
    data = await request.json()
    if not data:
        return JSONResponse({'success': False, 'error': '无数据'}, status_code=400)
    characters = load_characters()
    if name not in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '猫娘不存在'}, status_code=404)
    if 'live2d' in data:
        characters['猫娘'][name]['live2d'] = data['live2d']
    save_characters(characters)
    return {"success": True}

@app.put('/api/characters/catgirl/voice_id/{name}')
async def update_catgirl_voice_id(name: str, request: Request):
    data = await request.json()
    if not data:
        return JSONResponse({'success': False, 'error': '无数据'}, status_code=400)
    characters = load_characters()
    if name not in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '猫娘不存在'}, status_code=404)
    if 'voice_id' in data:
        characters['猫娘'][name]['voice_id'] = data['voice_id']
    save_characters(characters)
    return {"success": True}

@app.post('/api/tmpfiles_voice_clone')
async def tmpfiles_voice_clone(file: UploadFile = File(...), prefix: str = Form(...)):
    import os
    temp_path = f'tmp_{file.filename}'
    with open(temp_path, 'wb') as f:
        f.write(await file.read())
    tmp_url = None
    try:
        # 1. 上传到 tmpfiles.org
        with open(temp_path, 'rb') as f2:
            files = {'file': (file.filename, f2)}
            resp = requests.post('https://tmpfiles.org/api/v1/upload', files=files, timeout=30)
            data = resp.json()
            if not data or 'data' not in data or 'url' not in data['data']:
                return JSONResponse({'error': '上传到 tmpfiles.org 失败'}, status_code=500)
            page_url = data['data']['url']
            # 替换域名部分为直链
            if page_url.startswith('http://tmpfiles.org/'):
                tmp_url = page_url.replace('http://tmpfiles.org/', 'http://tmpfiles.org/dl/', 1)
            elif page_url.startswith('https://tmpfiles.org/'):
                tmp_url = page_url.replace('https://tmpfiles.org/', 'https://tmpfiles.org/dl/', 1)
            else:
                tmp_url = page_url  # 兜底
        # 2. 用直链注册音色
        dashscope.api_key = CORE_API_KEY
        service = VoiceEnrollmentService()
        target_model = "cosyvoice-v2"
        voice_id = service.create_voice(target_model=target_model, prefix=prefix, url=tmp_url)
        return JSONResponse({
            'voice_id': voice_id,
            'request_id': service.get_last_request_id(),
            'file_url': tmp_url
        })
    except Exception as e:
        return JSONResponse({'error': str(e), 'file_url': tmp_url}, status_code=500)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

@app.delete('/api/characters/catgirl/{name}')
async def delete_catgirl(name: str):
    characters = load_characters()
    if name not in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '猫娘不存在'}, status_code=404)
    del characters['猫娘'][name]
    save_characters(characters)
    return {"success": True}

@app.post('/api/characters/catgirl/{old_name}/rename')
async def rename_catgirl(old_name: str, request: Request):
    data = await request.json()
    new_name = data.get('new_name') if data else None
    if not new_name:
        return JSONResponse({'success': False, 'error': '新档案名不能为空'}, status_code=400)
    characters = load_characters()
    if old_name not in characters.get('猫娘', {}):
        return JSONResponse({'success': False, 'error': '原猫娘不存在'}, status_code=404)
    if new_name in characters['猫娘']:
        return JSONResponse({'success': False, 'error': '新档案名已存在'}, status_code=400)
    # 重命名
    characters['猫娘'][new_name] = characters['猫娘'].pop(old_name)
    save_characters(characters)
    return {"success": True}

@app.get("/{lanlan_name}", response_class=HTMLResponse)
async def get_index(request: Request, lanlan_name: str):
    # 每次动态获取角色数据
    _, _, _, lanlan_basic_config, _, _, _, _, _, _ = get_character_data()
    # 获取live2d字段
    live2d = lanlan_basic_config.get(lanlan_name, {}).get('live2d', 'mao_pro')
    # 查找所有模型
    models = find_models()
    # 根据live2d字段查找对应的model path
    model_path = next((m["path"] for m in models if m["name"] == live2d), f"/static/{live2d}/{live2d}.model3.json")
    return templates.TemplateResponse("templates/index.html", {
        "request": request,
        "lanlan_name": lanlan_name,
        "model_path": model_path
    })


# --- Run the Server ---
# (Keep your existing __main__ block)
if __name__ == "__main__":
    import uvicorn

    logger.info("--- Starting FastAPI Server ---")
    # Use os.path.abspath to show full path clearly
    logger.info(f"Serving static files from: {os.path.abspath('static')}")
    logger.info(f"Serving index.html from: {os.path.abspath('templates/index.html')}")
    logger.info(f"Access UI at: http://127.0.0.1:{MAIN_SERVER_PORT} (or your network IP:{MAIN_SERVER_PORT})")
    logger.info("-----------------------------")
    # Run from the directory containing server.py (gemini-live-app/)
    uvicorn.run("main_server:app", host="0.0.0.0", port=MAIN_SERVER_PORT, reload=False)

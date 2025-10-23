"""
TTS Helper模块
负责处理TTS语音合成，支持自定义音色（阿里云CosyVoice）和默认音色（各core_api的原生TTS）
"""
import numpy as np
from librosa import resample
import time
import asyncio
import json
import base64
import logging
import websockets
from enum import Enum
from multiprocessing import Queue as MPQueue, Process
import threading
import io
import wave
import aiohttp

logger = logging.getLogger(__name__)


def step_realtime_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    StepFun实时TTS worker（用于默认音色）
    使用阶跃星辰的实时TTS API（step-tts-mini）
    
    Args:
        request_queue: 多进程请求队列，接收(speech_id, text)元组
        response_queue: 多进程响应队列，发送音频数据
        audio_api_key: API密钥
        voice_id: 音色ID，默认使用"qingchunshaonv"
    """
    import asyncio
    
    # 使用默认音色 "qingchunshaonv"
    if not voice_id:
        voice_id = "qingchunshaonv"
    
    async def async_worker():
        """异步TTS worker主循环"""
        tts_url = "wss://api.stepfun.com/v1/realtime/audio?model=step-tts-mini"
        ws = None
        current_speech_id = None
        receive_task = None
        session_id = None
        session_ready = asyncio.Event()
        
        try:
            # 连接WebSocket
            headers = {"Authorization": f"Bearer {audio_api_key}"}
            
            ws = await websockets.connect(tts_url, additional_headers=headers)
            
            # 等待连接成功事件
            async def wait_for_connection():
                """等待连接成功"""
                nonlocal session_id
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        if event_type == "tts.connection.done":
                            session_id = event.get("data", {}).get("session_id")
                            session_ready.set()
                            break
                        elif event_type == "tts.response.error":
                            logger.error(f"TTS服务器错误: {event}")
                            break
                except Exception as e:
                    logger.error(f"等待连接时出错: {e}")
            
            # 等待连接成功
            try:
                await asyncio.wait_for(wait_for_connection(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("等待连接超时")
                return
            
            if not session_ready.is_set() or not session_id:
                logger.error("连接未能正确建立")
                return
            
            # 发送创建会话事件
            create_event = {
                "type": "tts.create",
                "data": {
                    "session_id": session_id,
                    "voice_id": voice_id,
                    "response_format": "wav",
                    "sample_rate": 24000
                }
            }
            await ws.send(json.dumps(create_event))
            
            # 等待会话创建成功
            async def wait_for_session_ready():
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        if event_type == "tts.response.created":
                            break
                        elif event_type == "tts.response.error":
                            logger.error(f"创建会话错误: {event}")
                            break
                except Exception as e:
                    logger.error(f"等待会话创建时出错: {e}")
            
            try:
                await asyncio.wait_for(wait_for_session_ready(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("会话创建超时")
            
            # 初始接收任务
            async def receive_messages_initial():
                """初始接收任务"""
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        if event_type == "tts.response.error":
                            logger.error(f"TTS错误: {event}")
                        elif event_type == "tts.response.audio.delta":
                            try:
                                # StepFun 返回 BASE64 编码的完整音频（包含 wav header）
                                audio_b64 = event.get("data", {}).get("audio", "")
                                if audio_b64:
                                    audio_bytes = base64.b64decode(audio_b64)
                                    # 使用 wave 模块读取 WAV 数据
                                    with io.BytesIO(audio_bytes) as wav_io:
                                        with wave.open(wav_io, 'rb') as wav_file:
                                            # 读取音频数据
                                            pcm_data = wav_file.readframes(wav_file.getnframes())
                                    
                                    # 转换为 numpy 数组
                                    audio_array = np.frombuffer(pcm_data, dtype=np.int16)
                                    # 重采样 24000Hz -> 48000Hz
                                    resampled = np.repeat(audio_array, 48000 // 24000)
                                    response_queue.put(resampled.tobytes())
                            except Exception as e:
                                logger.error(f"处理音频数据时出错: {e}")
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    logger.error(f"消息接收出错: {e}")
            
            receive_task = asyncio.create_task(receive_messages_initial())
            
            # 主循环：处理请求队列
            loop = asyncio.get_running_loop()
            while True:
                try:
                    sid, tts_text = await loop.run_in_executor(None, request_queue.get)
                except Exception:
                    break
                
                if sid is None:
                    # 提交缓冲区完成当前合成
                    if ws and session_id and current_speech_id is not None:
                        try:
                            done_event = {
                                "type": "tts.text.done",
                                "data": {"session_id": session_id}
                            }
                            await ws.send(json.dumps(done_event))
                        except Exception as e:
                            logger.error(f"完成生成失败: {e}")
                    continue
                
                # 新的语音ID，重新建立连接
                if current_speech_id != sid:
                    current_speech_id = sid
                    if ws:
                        try:
                            await ws.close()
                        except:
                            pass
                    if receive_task and not receive_task.done():
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            pass
                    
                    # 建立新连接
                    try:
                        ws = await websockets.connect(tts_url, additional_headers=headers)
                        
                        # 等待连接成功
                        session_id = None
                        session_ready.clear()
                        
                        async def wait_conn():
                            nonlocal session_id
                            try:
                                async for message in ws:
                                    event = json.loads(message)
                                    if event.get("type") == "tts.connection.done":
                                        session_id = event.get("data", {}).get("session_id")
                                        session_ready.set()
                                        break
                            except Exception:
                                pass
                        
                        try:
                            await asyncio.wait_for(wait_conn(), timeout=3.0)
                        except asyncio.TimeoutError:
                            logger.warning("新连接超时")
                            continue
                        
                        if not session_id:
                            continue
                        
                        # 创建会话
                        await ws.send(json.dumps({
                            "type": "tts.create",
                            "data": {
                                "session_id": session_id,
                                "voice_id": voice_id,
                                "response_format": "wav",
                                "sample_rate": 24000
                            }
                        }))
                        
                        # 启动新的接收任务
                        async def receive_messages():
                            try:
                                async for message in ws:
                                    event = json.loads(message)
                                    event_type = event.get("type")
                                    
                                    if event_type == "tts.response.error":
                                        logger.error(f"TTS错误: {event}")
                                    elif event_type == "tts.response.audio.delta":
                                        try:
                                            audio_b64 = event.get("data", {}).get("audio", "")
                                            if audio_b64:
                                                audio_bytes = base64.b64decode(audio_b64)
                                                # 使用 wave 模块读取 WAV 数据
                                                with io.BytesIO(audio_bytes) as wav_io:
                                                    with wave.open(wav_io, 'rb') as wav_file:
                                                        # 读取音频数据
                                                        pcm_data = wav_file.readframes(wav_file.getnframes())
                                                
                                                # 转换为 numpy 数组
                                                audio_array = np.frombuffer(pcm_data, dtype=np.int16)
                                                # 重采样 24000Hz -> 48000Hz
                                                resampled = np.repeat(audio_array, 48000 // 24000)
                                                response_queue.put(resampled.tobytes())
                                        except Exception as e:
                                            logger.error(f"处理音频数据时出错: {e}")
                            except websockets.exceptions.ConnectionClosed:
                                pass
                            except Exception as e:
                                logger.error(f"消息接收出错: {e}")
                        
                        receive_task = asyncio.create_task(receive_messages())
                        
                    except Exception as e:
                        logger.error(f"重新建立连接失败: {e}")
                        continue
                
                # 检查文本有效性
                if not tts_text or not tts_text.strip():
                    continue
                
                if not ws or not session_id:
                    continue
                
                # 发送文本
                try:
                    text_event = {
                        "type": "tts.text.delta",
                        "data": {
                            "session_id": session_id,
                            "text": tts_text
                        }
                    }
                    await ws.send(json.dumps(text_event))
                except Exception as e:
                    logger.error(f"发送TTS文本失败: {e}")
        
        except Exception as e:
            logger.error(f"StepFun实时TTS Worker错误: {e}")
        finally:
            # 清理资源
            if receive_task and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except asyncio.CancelledError:
                    pass
            
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
    
    # 运行异步worker
    try:
        asyncio.run(async_worker())
    except Exception as e:
        logger.error(f"StepFun实时TTS Worker启动失败: {e}")


def qwen_realtime_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    Qwen实时TTS worker（用于默认音色）
    使用阿里云的实时TTS API（qwen3-tts-flash-2025-09-18）
    
    Args:
        request_queue: 多进程请求队列，接收(speech_id, text)元组
        response_queue: 多进程响应队列，发送音频数据
        audio_api_key: API密钥
        voice_id: 音色ID，默认使用"Cherry"
    """
    import asyncio

    if not voice_id:
        voice_id = "Cherry"
    
    async def async_worker():
        """异步TTS worker主循环"""
        tts_url = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime-2025-09-18"
        ws = None
        current_speech_id = None
        receive_task = None
        session_ready = asyncio.Event()
        
        try:
            # 连接WebSocket
            headers = {"Authorization": f"Bearer {audio_api_key}"}
            
            # 配置会话消息模板（在重连时复用）
            # 使用 SERVER_COMMIT 模式：多次 append 文本，最后手动 commit 触发合成
            # 这样可以累积文本，避免"一个字一个字往外蹦"的问题
            config_message = {
                "type": "session.update",
                "event_id": f"event_{int(time.time() * 1000)}",
                "session": {
                    "mode": "server_commit",
                    "voice": voice_id,
                    "response_format": "pcm",
                    "sample_rate": 24000,
                    "channels": 1,
                    "bit_depth": 16
                }
            }
            
            ws = await websockets.connect(tts_url, additional_headers=headers)
            
            # 等待并处理初始消息
            async def wait_for_session_ready():
                """等待会话创建确认"""
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        # Qwen TTS API 返回 session.updated 而不是 session.created
                        if event_type in ["session.created", "session.updated"]:
                            session_ready.set()
                            break
                        elif event_type == "error":
                            logger.error(f"TTS服务器错误: {event}")
                            break
                except Exception as e:
                    logger.error(f"等待会话就绪时出错: {e}")
            
            # 发送配置
            await ws.send(json.dumps(config_message))
            
            # 等待会话就绪（超时5秒）
            try:
                await asyncio.wait_for(wait_for_session_ready(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("❌ 等待会话就绪超时")
                return
            
            if not session_ready.is_set():
                logger.error("❌ 会话未能正确初始化")
                return
            
            # 初始接收任务（会在每次新 speech_id 时重新创建）
            async def receive_messages_initial():
                """初始接收任务"""
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        if event_type == "error":
                            logger.error(f"TTS错误: {event}")
                        elif event_type == "response.audio.delta":
                            try:
                                audio_bytes = base64.b64decode(event.get("delta", ""))
                                audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                                resampled = np.repeat(audio_array, 2)
                                response_queue.put(resampled.tobytes())
                            except Exception as e:
                                logger.error(f"处理音频数据时出错: {e}")
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    logger.error(f"消息接收出错: {e}")
            
            receive_task = asyncio.create_task(receive_messages_initial())
            
            # 主循环：处理请求队列
            loop = asyncio.get_running_loop()
            while True:
                # 非阻塞检查队列
                try:
                    sid, tts_text = await loop.run_in_executor(None, request_queue.get)
                except Exception:
                    break
                
                if sid is None:
                    # 提交缓冲区完成当前合成（仅当之前有文本时）
                    if ws and session_ready.is_set() and current_speech_id is not None:
                        try:
                            await ws.send(json.dumps({
                                "type": "input_text_buffer.commit",
                                "event_id": f"event_{int(time.time() * 1000)}_interrupt_commit"
                            }))
                        except Exception as e:
                            logger.error(f"提交缓冲区失败: {e}")
                    continue
                
                # 新的语音ID，重新建立连接（类似 speech_synthesis_worker 的逻辑）
                # 直接关闭旧连接，打断旧语音
                if current_speech_id != sid:
                    current_speech_id = sid
                    if ws:
                        try:
                            await ws.close()
                        except:
                            pass
                    if receive_task and not receive_task.done():
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            pass
                    
                    # 建立新连接
                    try:
                        ws = await websockets.connect(tts_url, additional_headers=headers)
                        await ws.send(json.dumps(config_message))
                        
                        # 等待 session.created
                        session_ready.clear()
                        
                        async def wait_ready():
                            try:
                                async for message in ws:
                                    event = json.loads(message)
                                    event_type = event.get("type")
                                    # Qwen TTS API 返回 session.updated 而不是 session.created
                                    if event_type in ["session.created", "session.updated"]:
                                        session_ready.set()
                                        break
                                    elif event_type == "error":
                                        logger.error(f"等待期间收到错误: {event}")
                                        break
                            except Exception as e:
                                logger.error(f"wait_ready 异常: {e}")
                        
                        try:
                            await asyncio.wait_for(wait_ready(), timeout=2.0)
                        except asyncio.TimeoutError:
                            logger.warning("新会话创建超时")
                        
                        # 启动新的接收任务
                        async def receive_messages():
                            try:
                                async for message in ws:
                                    event = json.loads(message)
                                    event_type = event.get("type")
                                    
                                    if event_type == "error":
                                        logger.error(f"TTS错误: {event}")
                                    elif event_type == "response.audio.delta":
                                        try:
                                            audio_bytes = base64.b64decode(event.get("delta", ""))
                                            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                                            resampled = np.repeat(audio_array, 2)
                                            response_queue.put(resampled.tobytes())
                                        except Exception as e:
                                            logger.error(f"处理音频数据时出错: {e}")
                            except websockets.exceptions.ConnectionClosed:
                                pass
                            except Exception as e:
                                logger.error(f"消息接收出错: {e}")
                        
                        receive_task = asyncio.create_task(receive_messages())
                        
                    except Exception as e:
                        logger.error(f"重新建立连接失败: {e}")
                        continue
                
                # 检查文本有效性
                if not tts_text or not tts_text.strip():
                    continue
                
                if not ws or not session_ready.is_set():
                    continue
                
                # 追加文本到缓冲区（不立即提交，等待响应完成时的终止信号再 commit）
                try:
                    await ws.send(json.dumps({
                        "type": "input_text_buffer.append",
                        "event_id": f"event_{int(time.time() * 1000)}",
                        "text": tts_text
                    }))
                except Exception as e:
                    logger.error(f"发送TTS文本失败: {e}")
        
        except Exception as e:
            logger.error(f"Qwen实时TTS Worker错误: {e}")
        finally:
            # 清理资源
            if receive_task and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except asyncio.CancelledError:
                    pass
            
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
    
    # 运行异步worker
    try:
        asyncio.run(async_worker())
    except Exception as e:
        logger.error(f"Qwen实时TTS Worker启动失败: {e}")


def cosyvoice_vc_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    TTS多进程worker函数，用于阿里云CosyVoice TTS
    
    Args:
        request_queue: 多进程请求队列，接收(speech_id, text)元组
        response_queue: 多进程响应队列，发送音频数据
        audio_api_key: API密钥
        voice_id: 音色ID
    """
    import dashscope
    from dashscope.audio.tts_v2 import ResultCallback, SpeechSynthesizer, AudioFormat
    
    dashscope.api_key = audio_api_key
    
    class Callback(ResultCallback):
        def __init__(self, response_queue):
            self.response_queue = response_queue
            self.cache = np.zeros(0).astype(np.float32)
            
        def on_open(self): 
            pass
            
        def on_complete(self): 
            if len(self.cache) > 0:
                data = (resample(self.cache, orig_sr=24000, target_sr=48000) * 32768.).clip(-32768, 32767).astype(np.int16).tobytes()
                self.response_queue.put(data)
                self.cache = np.zeros(0).astype(np.float32)
                
        def on_error(self, message: str): 
            print(f"TTS Error: {message}")
            
        def on_close(self): 
            pass
            
        def on_event(self, message): 
            pass
            
        def on_data(self, data: bytes) -> None:
            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self.cache = np.concatenate([self.cache, audio])
            if len(self.cache) >= 8000:
                data = self.cache[:8000]
                data = (resample(data, orig_sr=24000, target_sr=48000) * 32768.).clip(-32768, 32767).astype(np.int16).tobytes()
                self.response_queue.put(data)
                self.cache = self.cache[8000:]
            
    callback = Callback(response_queue)
    current_speech_id = None
    synthesizer = None
    
    while True:
        # 非阻塞检查队列，优先处理打断
        if request_queue.empty():
            time.sleep(0.01)
            continue

        sid, tts_text = request_queue.get()

        if sid is None:
            # 停止当前合成
            if synthesizer is not None:
                try:
                    synthesizer.streaming_complete()
                except Exception:
                    synthesizer = None
            continue
            
        if current_speech_id is None or current_speech_id != sid or synthesizer is None:
            current_speech_id = sid
            try:
                if synthesizer is not None:
                    try:
                        synthesizer.close()
                    except Exception:
                        pass
                synthesizer = SpeechSynthesizer(
                    model="cosyvoice-v2",
                    voice=voice_id,
                    speech_rate=1.1,
                    format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                    callback=callback,
                )
            except Exception as e:
                print("TTS Error: ", e)
                synthesizer = None
                current_speech_id = None
                continue
                
        if tts_text is None or not tts_text.strip():
            time.sleep(0.01)
            continue
            
        # 处理表情等逻辑
        try:
            synthesizer.streaming_call(tts_text)
        except Exception as e:
            print("TTS Error: ", e)
            synthesizer = None
            current_speech_id = None
            continue


def cogtts_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    智谱AI CogTTS worker（用于默认音色）
    使用智谱AI的CogTTS API（cogtts）
    注意：CogTTS不支持流式输入，只支持流式输出
    因此需要累积文本后一次性发送，但可以流式接收音频
    
    Args:
        request_queue: 多进程请求队列，接收(speech_id, text)元组
        response_queue: 多进程响应队列，发送音频数据
        audio_api_key: API密钥
        voice_id: 音色ID，默认使用"tongtong"（支持：tongtong, chuichui, xiaochen, jam, kazi, douji, luodo）
    """
    import asyncio
    
    # 使用默认音色 "tongtong"
    if not voice_id:
        voice_id = "tongtong"
    
    async def async_worker():
        """异步TTS worker主循环"""
        tts_url = "https://open.bigmodel.cn/api/paas/v4/audio/speech"
        current_speech_id = None
        text_buffer = []  # 累积文本缓冲区
        
        try:
            loop = asyncio.get_running_loop()
            
            while True:
                try:
                    sid, tts_text = await loop.run_in_executor(None, request_queue.get)
                except Exception:
                    break
                
                # 新的语音ID，清空缓冲区并重新开始
                if current_speech_id != sid and sid is not None:
                    current_speech_id = sid
                    text_buffer = []
                
                if sid is None:
                    # 收到终止信号，合成累积的文本
                    if text_buffer and current_speech_id is not None:
                        full_text = "".join(text_buffer)
                        if full_text.strip():
                            try:
                                # 发送HTTP请求进行TTS合成
                                headers = {
                                    "Authorization": f"Bearer {audio_api_key}",
                                    "Content-Type": "application/json"
                                }
                                
                                payload = {
                                    "model": "cogtts",
                                    "input": full_text[:1024],  # CogTTS最大支持1024字符
                                    "voice": voice_id,
                                    "response_format": "pcm",
                                    "encode_format": "base64",  # 返回base64编码的PCM
                                    "speed": 1.0,
                                    "volume": 1.0,
                                    "stream": True,
                                }
                                
                                # 使用异步HTTP客户端流式接收SSE响应
                                async with aiohttp.ClientSession() as session:
                                    async with session.post(tts_url, headers=headers, json=payload) as resp:
                                        if resp.status == 200:
                                            # CogTTS返回SSE格式: data: {...JSON...}
                                            # 使用缓冲区逐块读取，避免 "Chunk too big" 错误
                                            buffer = ""
                                            first_audio_received = False  # 用于调试第一个音频块
                                            async for chunk in resp.content.iter_any():
                                                # 解码并添加到缓冲区
                                                buffer += chunk.decode('utf-8')
                                                
                                                # 按行分割处理
                                                while '\n' in buffer:
                                                    line, buffer = buffer.split('\n', 1)
                                                    line = line.strip()
                                                    
                                                    # 跳过空行
                                                    if not line:
                                                        continue
                                                    
                                                    # 解析SSE格式: data: {...}
                                                    if line.startswith('data: '):
                                                        json_str = line[6:]  # 去掉 "data: " 前缀
                                                        try:
                                                            event_data = json.loads(json_str)
                                                            
                                                            # 提取音频数据: choices[0].delta.content
                                                            choices = event_data.get('choices', [])
                                                            if choices and 'delta' in choices[0]:
                                                                delta = choices[0]['delta']
                                                                audio_b64 = delta.get('content', '')
                                                                
                                                                if audio_b64:
                                                                    # Base64解码得到PCM数据
                                                                    audio_bytes = base64.b64decode(audio_b64)
                                                                    
                                                                    # 跳过过小的音频块（可能是初始化数据）
                                                                    # 至少需要 100 个采样点（约 4ms@24kHz）才处理
                                                                    if len(audio_bytes) < 200:  # 100 samples * 2 bytes
                                                                        logger.debug(f"跳过过小的音频块: {len(audio_bytes)} bytes")
                                                                        continue
                                                                    
                                                                    # CogTTS返回PCM格式（24000Hz, mono, 16bit）
                                                                    # 从返回的 return_sample_rate 获取采样率
                                                                    sample_rate = delta.get('return_sample_rate', 24000)
                                                                    
                                                                    # 转换为 float32 进行高质量重采样
                                                                    audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                                                                    
                                                                    # 对第一个音频块，裁剪掉开头的噪音部分（CogTTS有初始化噪音）
                                                                    if not first_audio_received:
                                                                        first_audio_received = True
                                                                        # 裁剪掉前 1s 的音频（通常包含初始化噪音）
                                                                        trim_samples = int(sample_rate)
                                                                        if len(audio_array) > trim_samples:
                                                                            audio_array = audio_array[trim_samples:]
                                                                            logger.debug(f"裁剪第一个音频块的前 {trim_samples} 个采样点（{trim_samples/sample_rate:.2f}秒）")
                                                                        # 对裁剪后的开头应用短淡入（10ms），平滑过渡
                                                                        fade_samples = min(int(sample_rate * 0.01), len(audio_array))
                                                                        if fade_samples > 0:
                                                                            fade_curve = np.linspace(0.0, 1.0, fade_samples)
                                                                            audio_array[:fade_samples] *= fade_curve
                                                                    
                                                                    # 使用 librosa 进行高质量重采样
                                                                    resampled = resample(audio_array, orig_sr=sample_rate, target_sr=48000)
                                                                    # 转回 int16 格式
                                                                    resampled_int16 = (resampled * 32768.0).clip(-32768, 32767).astype(np.int16)
                                                                    response_queue.put(resampled_int16.tobytes())
                                                        except json.JSONDecodeError as e:
                                                            logger.warning(f"解析SSE JSON失败: {e}")
                                                        except Exception as e:
                                                            logger.error(f"处理音频数据时出错: {e}")
                                        else:
                                            error_text = await resp.text()
                                            logger.error(f"CogTTS API错误 ({resp.status}): {error_text}")
                            except Exception as e:
                                logger.error(f"CogTTS合成失败: {e}")
                    
                    # 清空缓冲区
                    text_buffer = []
                    continue
                
                # 累积文本到缓冲区（不立即发送）
                if tts_text and tts_text.strip():
                    text_buffer.append(tts_text)
        
        except Exception as e:
            logger.error(f"CogTTS Worker错误: {e}")
    
    # 运行异步worker
    try:
        asyncio.run(async_worker())
    except Exception as e:
        logger.error(f"CogTTS Worker启动失败: {e}")


def dummy_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    空的TTS worker（用于不支持TTS的core_api）
    持续清空请求队列但不生成任何音频，使程序正常运行但无语音输出
    
    Args:
        request_queue: 多进程请求队列，接收(speech_id, text)元组
        response_queue: 多进程响应队列（不使用）
        audio_api_key: API密钥（不使用）
        voice_id: 音色ID（不使用）
    """
    logger.warning("TTS Worker 未启用，不会生成语音")
    
    while True:
        try:
            # 持续清空队列以避免阻塞，但不做任何处理
            sid, tts_text = request_queue.get()
            # 如果收到结束信号，继续等待下一个请求
            if sid is None:
                continue
        except Exception as e:
            logger.error(f"Dummy TTS Worker 错误: {e}")
            break


def get_tts_worker(core_api_type='qwen', has_custom_voice=False):
    """
    根据 core_api 类型和是否有自定义音色，返回对应的 TTS worker 函数
    
    Args:
        core_api_type: core API 类型 ('qwen', 'step', 'glm' 等)
        has_custom_voice: 是否有自定义音色 (voice_id)
    
    Returns:
        对应的 TTS worker 函数
    """
    # 如果有自定义音色，使用 CosyVoice（仅阿里云支持）
    if has_custom_voice:
        return cosyvoice_vc_tts_worker
    
    # 没有自定义音色时，使用与 core_api 匹配的默认 TTS
    if core_api_type == 'qwen':
        return qwen_realtime_tts_worker
    elif core_api_type == 'step':
        return step_realtime_tts_worker
    elif core_api_type == 'glm':
        return cogtts_tts_worker
    else:
        logger.error(f"{core_api_type}不支持原生TTS，请使用自定义语音")
        return dummy_tts_worker

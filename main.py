from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import Response
import websockets
from websockets.protocol import State
import traceback
import requests
import audioop
import asyncio
import base64
import json
import redis

# =======================
# Configuration & Prompts
# =======================

# Production public URL (HTTPS); NGINX will proxy HTTPS to Gunicorn on port 80.
PUBLIC_URL = "https://ajingolik.fun"  # Your domain with SSL.
ULTRAVOX_API_KEY = "UynTT05e.At9rcD0UNmpPMd0jaAW40pEnubRHX3mB"
N8N_WEBHOOK_URL = "https://primary-production-e5ec.up.railway.app/webhook/RealtimeAPIFunctions"
PORT = 80  # Gunicorn will listen on port 80.

# Ultravox configuration.
ULTRAVOX_MODEL = "fixie-ai/ultravox-70B"
ULTRAVOX_VOICE = "Tanya-English"  # or "Mark"
ULTRAVOX_SAMPLE_RATE = 8000        
ULTRAVOX_BUFFER_SIZE = 60        

# System prompt.
SYSTEM_MESSAGE = (
    "Hello, you are Sara from Agenix AI solutions. "
    "Greet the customer and ask how you can help them. "
    "Keep the conversation professional and helpful."
)

# Calendars list for scheduling meetings.
CALENDARS_LIST = {
    "LOCATION1": "CALENDAR_EMAIL1",
    "LOCATION2": "CALENDAR_EMAIL2",
    "LOCATION3": "CALENDAR_EMAIL3",
}

# =======================
# Redis Session Store Setup
# =======================
# Ensure Redis is installed and running (default host and port).
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# =======================
# FastAPI Application Setup
# =======================
app = FastAPI()

# (We no longer use an in-memory dictionary; sessions are stored in Redis.)

# Event types for logging.
LOG_EVENT_TYPES = [
    'response.content.done',
    'response.done',
    'session.created',
    'conversation.item.input_audio_transcription.completed'
]

# Helper: Convert an HTTPS URL to a secure WebSocket URL.
def to_websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    else:
        return url

# Helper: Save session data in Redis.
def save_session(call_sid: str, session: dict):
    redis_client.set(f"session:{call_sid}", json.dumps(session))

# Helper: Retrieve session data from Redis.
def get_session(call_sid: str) -> dict:
    data = redis_client.get(f"session:{call_sid}")
    if data:
        return json.loads(data)
    return None

# Helper: Delete session data.
def delete_session(call_sid: str):
    redis_client.delete(f"session:{call_sid}")

# =======================
# Routes and Endpoints
# =======================

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

@app.get("/")
async def root():
    return {"message": "Twilio + Ultravox Media Stream Server is running!"}

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Handles inbound calls from Twilio:
      - Reads form parameters.
      - Fetches the first message from N8N.
      - Saves session data in Redis.
      - Returns TwiML instructing Twilio to connect via a secure WebSocket.
    """
    form_data = await request.form()
    twilio_params = dict(form_data)
    print("Incoming call received.")
    
    caller_number = twilio_params.get("From", "Unknown")
    session_id = twilio_params.get("CallSid")
    print(f"Caller Number: {caller_number}")
    print(f"Session ID (CallSid): {session_id}")

    # Default first message.
    first_message = (
        "You just picked up the phone for a custom. This is the first time the customer is calling. "
        "Start with a fresh greeting. Introduce yourself as Sara from Agenix AI and ask what you can help them with."
    )
    
    print("Fetching first message from N8N ...")
    try:
        webhook_response = requests.post(
            N8N_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            json={"route": "1", "number": caller_number, "data": "empty"},
            timeout=10
        )
        if webhook_response.ok:
            response_text = webhook_response.text
            try:
                response_data = json.loads(response_text)
                if response_data and response_data.get("firstMessage"):
                    first_message = response_data["firstMessage"]
                    print("Parsed firstMessage from N8N:", first_message)
            except json.JSONDecodeError:
                first_message = response_text.strip()
        else:
            print("N8N webhook returned error:", webhook_response.status_code)
    except Exception as e:
        print("Error sending data to N8N webhook:", e)
    
    session = {
        "transcript": "",
        "callerNumber": caller_number,
        "callDetails": twilio_params,
        "firstMessage": first_message,
        "streamSid": None
    }
    save_session(session_id, session)

    # Build secure WebSocket URL.
    ws_url = to_websocket_url(PUBLIC_URL)
    stream_url = f"{ws_url}/media-stream"
    print("Responding with TwiML. Stream URL:", stream_url)

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}">
            <Parameter name="firstMessage" value="{first_message}" />
            <Parameter name="callerNumber" value="{caller_number}" />
            <Parameter name="callSid" value="{session_id}" />
        </Stream>
    </Connect>
</Response>"""
    return Response(content=twiml_response, media_type="text/xml")

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Handles the WebSocket connection from Twilio Media Streams.
    After accepting the connection, waits for the "start" event,
    creates an Ultravox call, and bridges audio data.
    """
    await websocket.accept()
    print("WebSocket connection accepted at /media-stream (Twilio).")

    call_sid = None
    session = None
    stream_sid = ""
    uv_ws = None  # Ultravox WebSocket connection

    async def handle_ultravox():
        nonlocal uv_ws, session, stream_sid, call_sid
        try:
            async for raw_message in uv_ws:
                if isinstance(raw_message, bytes):
                    try:
                        mu_law_bytes = audioop.lin2ulaw(raw_message, 2)
                        payload_base64 = base64.b64encode(mu_law_bytes).decode("ascii")
                    except Exception as e:
                        print("Error transcoding PCM to µ-law:", e)
                        continue
                    try:
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload_base64}
                        }))
                    except Exception as e:
                        print("Error sending media to Twilio:", e)
                else:
                    try:
                        msg_data = json.loads(raw_message)
                    except Exception as e:
                        print("Received non-JSON data from Ultravox:", raw_message)
                        continue

                    msg_type = msg_data.get("type") or msg_data.get("eventType")
                    if msg_type == "transcript":
                        role = msg_data.get("role")
                        text = msg_data.get("text") or msg_data.get("delta")
                        final = msg_data.get("final", False)
                        if role and text:
                            role_cap = role.capitalize()
                            session["transcript"] += f"{role_cap}: {text}\n"
                            print(f"{role_cap} says: {text}")
                            if final:
                                print(f"Transcript for {role_cap} finalized.")
                    elif msg_type == "client_tool_invocation":
                        toolName = msg_data.get("toolName", "")
                        invocationId = msg_data.get("invocationId")
                        parameters = msg_data.get("parameters", {})
                        print(f"Invoking tool: {toolName} with invocationId: {invocationId} and parameters: {parameters}")
                        if toolName == "question_and_answer":
                            question = parameters.get("question")
                            await handle_question_and_answer(uv_ws, invocationId, question)
                        elif toolName == "schedule_meeting":
                            required_params = ["name", "email", "purpose", "datetime", "location"]
                            missing_params = [param for param in required_params if not parameters.get(param)]
                            if missing_params:
                                prompt_message = f"Please provide: {', '.join(missing_params)}."
                                tool_result = {
                                    "type": "client_tool_result",
                                    "invocationId": invocationId,
                                    "result": prompt_message,
                                    "response_type": "tool-response"
                                }
                                await uv_ws.send(json.dumps(tool_result))
                            else:
                                await handle_schedule_meeting(uv_ws, session, invocationId, parameters)
                    elif msg_type == "state":
                        state = msg_data.get("state")
                        if state:
                            print("Ultravox state:", state)
                    elif msg_type == "debug":
                        debug_message = msg_data.get("message")
                        print("Ultravox debug message:", debug_message)
                    elif msg_type in LOG_EVENT_TYPES:
                        print(f"Ultravox event: {msg_type} - {msg_data}")
                    else:
                        print(f"Unhandled Ultravox message type: {msg_type} - {msg_data}")
        except Exception as e:
            print("Error in handle_ultravox:", e)
            traceback.print_exc()

    async def handle_twilio():
        nonlocal call_sid, session, stream_sid, uv_ws
        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)
                if data.get("event") == "start":
                    stream_sid = data["start"]["streamSid"]
                    call_sid = data["start"]["callSid"]
                    custom_parameters = data["start"].get("customParameters", {})
                    print("Received start event from Twilio:", data)
                    first_message = custom_parameters.get("firstMessage", "Hello, how can I assist you?")
                    caller_number = custom_parameters.get("callerNumber", "Unknown")
                    session = get_session(call_sid)
                    if not session:
                        print(f"Session not found for CallSid: {call_sid}")
                        await websocket.close()
                        return
                    # Update session with new values.
                    session["callerNumber"] = caller_number
                    session["streamSid"] = stream_sid
                    save_session(call_sid, session)
                    uv_join_url = await create_ultravox_call(system_prompt=SYSTEM_MESSAGE, first_message=first_message)
                    if not uv_join_url:
                        print("Ultravox joinUrl is empty. Cannot establish WebSocket connection.")
                        await websocket.close()
                        return
                    try:
                        uv_ws = await websockets.connect(uv_join_url)
                        print("Ultravox WebSocket connected.")
                    except Exception as e:
                        print("Error connecting to Ultravox WebSocket:", e)
                        traceback.print_exc()
                        await websocket.close()
                        return
                    asyncio.create_task(handle_ultravox())
                elif data.get("event") == "media":
                    payload_base64 = data["media"]["payload"]
                    try:
                        mu_law_bytes = base64.b64decode(payload_base64)
                    except Exception as e:
                        print("Error decoding base64 payload:", e)
                        continue
                    try:
                        pcm_bytes = audioop.ulaw2lin(mu_law_bytes, 2)
                    except Exception as e:
                        print("Error transcoding µ-law to PCM:", e)
                        continue
                    if uv_ws and uv_ws.state == State.OPEN:
                        try:
                            await uv_ws.send(pcm_bytes)
                        except Exception as e:
                            print("Error sending PCM to Ultravox:", e)
        except WebSocketDisconnect:
            print(f"Twilio WebSocket disconnected (CallSid={call_sid}).")
            if uv_ws and uv_ws.state == State.OPEN:
                await uv_ws.close()
            if session:
                await send_transcript_to_n8n(session)
                delete_session(call_sid)
        except Exception as e:
            print("Error in handle_twilio:", e)
            traceback.print_exc()

    await asyncio.create_task(handle_twilio())

async def create_ultravox_call(system_prompt: str, first_message: str) -> str:
    url = "https://api.ultravox.ai/api/calls"
    headers = {"X-API-Key": ULTRAVOX_API_KEY, "Content-Type": "application/json"}
    payload = {
        "systemPrompt": system_prompt,
        "model": ULTRAVOX_MODEL,
        "voice": ULTRAVOX_VOICE,
        "temperature": 0.1,
        "initialMessages": [{"role": "MESSAGE_ROLE_USER", "text": first_message}],
        "medium": {
            "serverWebSocket": {
                "inputSampleRate": ULTRAVOX_SAMPLE_RATE,
                "outputSampleRate": ULTRAVOX_SAMPLE_RATE,
                "clientBufferSizeMs": ULTRAVOX_BUFFER_SIZE
            }
        },
        "selectedTools": [
            {
                "temporaryTool": {
                    "modelToolName": "question_and_answer",
                    "description": "Get answers to customer questions.",
                    "dynamicParameters": [
                        {
                            "name": "question",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "description": "Question to be answered"},
                            "required": True
                        }
                    ],
                    "timeout": "20s",
                    "client": {}
                }
            },
            {
                "temporaryTool": {
                    "modelToolName": "schedule_meeting",
                    "description": "Schedule a meeting for a customer.",
                    "dynamicParameters": [
                        {
                            "name": "name",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "description": "Customer's name"},
                            "required": True
                        },
                        {
                            "name": "email",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "description": "Customer's email"},
                            "required": True
                        },
                        {
                            "name": "purpose",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "description": "Purpose of the Meeting"},
                            "required": True
                        },
                        {
                            "name": "datetime",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "description": "Meeting Datetime"},
                            "required": True
                        },
                        {
                            "name": "location",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {"type": "string", "enum": ["London", "Manchester", "Brighton"], "description": "Meeting location"},
                            "required": True
                        }
                    ],
                    "timeout": "20s",
                    "client": {}
                }
            }
        ]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        print("Ultravox API response status:", resp.status_code)
        print("Ultravox API response body:", resp.text)
        if not resp.ok:
            print("Ultravox create call error:", resp.status_code, resp.text)
            return ""
        body = resp.json()
        join_url = body.get("joinUrl", "")
        print("Ultravox joinUrl received:", join_url)
        return join_url
    except Exception as e:
        print("Ultravox create call request failed:", e)
        return ""

async def handle_question_and_answer(uv_ws, invocationId: str, question: str):
    try:
        answer_message = f"This is a placeholder answer for your question: '{question}'."
        tool_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "result": answer_message,
            "response_type": "tool-response"
        }
        await uv_ws.send(json.dumps(tool_result))
        print("Sent placeholder answer for question_and_answer.")
    except Exception as e:
        print("Error in question_and_answer tool:", e)
        error_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "error_type": "implementation-error",
            "error_message": "An error occurred while processing your request."
        }
        await uv_ws.send(json.dumps(error_result))

async def handle_schedule_meeting(uv_ws, session, invocationId: str, parameters):
    try:
        name = parameters.get("name")
        email = parameters.get("email")
        purpose = parameters.get("purpose")
        datetime_str = parameters.get("datetime")
        location = parameters.get("location")
        print(f"Schedule meeting parameters: name={name}, email={email}, purpose={purpose}, datetime={datetime_str}, location={location}")
        if not all([name, email, purpose, datetime_str, location]):
            raise ValueError("Missing one or more required parameters.")
        calendar_id = CALENDARS_LIST.get(location)
        if not calendar_id:
            raise ValueError(f"Invalid location: {location}")
        data = {
            "name": name,
            "email": email,
            "purpose": purpose,
            "datetime": datetime_str,
            "calendar_id": calendar_id
        }
        payload = {
            "route": "3",
            "number": session.get("callerNumber", "Unknown"),
            "data": json.dumps(data)
        }
        print("Sending scheduling payload to N8N:", json.dumps(payload, indent=2))
        webhook_response = await send_to_webhook(payload)
        parsed_response = json.loads(webhook_response)
        booking_message = parsed_response.get("message", "I'm sorry, I couldn't schedule the meeting at this time.")
        tool_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "result": booking_message,
            "response_type": "tool-response"
        }
        await uv_ws.send(json.dumps(tool_result))
        print("Sent schedule_meeting result to Ultravox:", booking_message)
    except Exception as e:
        print("Error scheduling meeting:", e)
        error_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "error_type": "implementation-error",
            "error_message": "An error occurred while scheduling your meeting."
        }
        await uv_ws.send(json.dumps(error_result))

async def send_transcript_to_n8n(session):
    print("Full Transcript:\n", session.get("transcript", ""))
    await send_to_webhook({
        "route": "2",
        "number": session.get("callerNumber", "Unknown"),
        "data": session.get("transcript", "")
    })

async def send_to_webhook(payload):
    if not N8N_WEBHOOK_URL:
        print("Error: N8N_WEBHOOK_URL is not set")
        return json.dumps({"error": "N8N_WEBHOOK_URL not configured"})
    try:
        print("Sending payload to N8N webhook:", json.dumps(payload, indent=2))
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if response.status_code != 200:
            print("N8N webhook returned status code", response.status_code)
            print("Response:", response.text)
            return json.dumps({"error": f"N8N webhook returned status {response.status_code}"})
        return response.text
    except requests.exceptions.RequestException as e:
        error_msg = f"Error sending data to N8N webhook: {str(e)}"
        print(error_msg)
        return json.dumps({"error": error_msg})

# =======================
# Application Entry Point
# =======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

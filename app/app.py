from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from openai import OpenAI
from typing_extensions import override
from openai import AssistantEventHandler
import re
import uuid
from .config import Config
import threading

app = Flask(__name__)
app.config.from_object(Config)
socketio = SocketIO(app)
client = OpenAI(api_key=app.config['OPENAI_API_KEY'])

# Global variable to control streaming
stop_streaming = threading.Event()

class EventHandler(AssistantEventHandler):
    @override
    def on_text_created(self, text) -> None:
        pass

    @override
    def on_text_delta(self, delta, snapshot):
        if stop_streaming.is_set():
            return
        clean_delta = clean_response(delta.value)
        emit('response', {'data': clean_delta}, broadcast=True)

    def on_tool_call_created(self, tool_call):
        pass

    def on_tool_call_delta(self, delta, snapshot):
        if stop_streaming.is_set():
            return
        if delta.type == 'code_interpreter':
            if delta.code_interpreter.input:
                clean_input = clean_response(delta.code_interpreter.input)
                emit('response', {'data': clean_input}, broadcast=True)
            if delta.code_interpreter.outputs:
                emit('response', {'data': "<br><br>output >"}, broadcast=True)
                for output in delta.code_interpreter.outputs:
                    if output.type == "logs":
                        clean_logs = clean_response(output.logs)
                        emit('response', {'data': clean_logs}, broadcast=True)

def create_assistant(instructions, data_name, assistant_name, model=app.config['MODEL']):
    vector_store_id = app.config['VECTOR_STORE_ID']
    assistant = client.beta.assistants.create(
        name=assistant_name,
        instructions=instructions,
        model=model,
        tools=[{"type": "file_search"}],
    )
    assistant = client.beta.assistants.update(
        assistant_id=assistant.id,
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
    )
    return assistant

def stream_answer(query, assistant, data_name):
    global stop_streaming
    stop_streaming.clear()
    
    vector_store_id = app.config['VECTOR_STORE_ID']
    thread = client.beta.threads.create(
        messages=[{"role": "user", "content": query, "attachments": []}],
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
    )

    try:
        with client.beta.threads.runs.stream(
            thread_id=thread.id,
            assistant_id=assistant.id,
            instructions=assistant.instructions,
            event_handler=EventHandler(),
        ) as stream:
            stream.until_done()
    except Exception as e:
        print(f"Error in stream_answer: {e}")
    finally:
        if not stop_streaming.is_set():
            emit('stream_complete', broadcast=True)

def clean_response(response):
    response = re.sub(r'【\d+:\d+†source】', '', response)
    response = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', response)
    response = response.replace('**', '')
    response = response.replace('\n', '<br>')
    return response

assistant = create_assistant(app.config['INSTRUCTIONS'], app.config['DATA_NAME'], app.config['ASSISTANT_NAME'])

@app.route('/')
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
    return render_template('index.html')

@socketio.on('query')
def handle_query(query):
    emit('clear_response', broadcast=True)
    stream_answer(query, assistant, app.config['DATA_NAME'])

@socketio.on('stop_generating')
def handle_stop_generating():
    global stop_streaming
    stop_streaming.set()
    emit('stream_complete', broadcast=True)

@socketio.on('connect')
def handle_connect():
    session_id = session.get('user_id')
    socketio.emit('session', {'session_id': session_id}, room=request.sid)

if __name__ == '__main__':
    socketio.run(app)
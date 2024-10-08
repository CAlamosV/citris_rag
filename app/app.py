from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room
from openai import OpenAI
from typing_extensions import override
from openai import AssistantEventHandler
import re
import uuid
from .config import Config
import threading
from collections import defaultdict

app = Flask(__name__)
app.config.from_object(Config)
socketio = SocketIO(app)
client = OpenAI(api_key=app.config['OPENAI_API_KEY'])

# Class to maintain per-user streaming state
class StreamingState:
    def __init__(self):
        self.stop_streaming = threading.Event()
        self.current_query_id = None

# Dictionary to store streaming states per user
user_streaming_states = defaultdict(StreamingState)

class EventHandler(AssistantEventHandler):
    def __init__(self, room, query_id, state):
        super().__init__()
        self.room = room
        self.query_id = query_id
        self.state = state

    @override
    def on_text_created(self, text) -> None:
        pass

    @override
    def on_text_delta(self, delta, snapshot):
        if self.state.stop_streaming.is_set() or self.state.current_query_id != self.query_id:
            raise Exception("Streaming stopped")
        clean_delta = clean_response(delta.value)
        socketio.emit('response', {'data': clean_delta}, room=self.room)

    def on_tool_call_created(self, tool_call):
        pass

    def on_tool_call_delta(self, delta, snapshot):
        if self.state.stop_streaming.is_set() or self.state.current_query_id != self.query_id:
            raise Exception("Streaming stopped")
        if delta.type == 'code_interpreter':
            if delta.code_interpreter.input:
                clean_input = clean_response(delta.code_interpreter.input)
                socketio.emit('response', {'data': clean_input}, room=self.room)
            if delta.code_interpreter.outputs:
                socketio.emit('response', {'data': "<br><br>output >"}, room=self.room)
                for output in delta.code_interpreter.outputs:
                    if output.type == "logs":
                        clean_logs = clean_response(output.logs)
                        socketio.emit('response', {'data': clean_logs}, room=self.room)

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

def stream_answer(query, assistant, data_name, room, query_id, state):
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
            event_handler=EventHandler(room, query_id, state),
        ) as stream:
            stream.until_done()
    except Exception as e:
        print(f"Error in stream_answer: {e}")
    finally:
        if not state.stop_streaming.is_set() and state.current_query_id == query_id:
            socketio.emit('stream_complete', room=room)

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
    room = session.get('user_id')
    join_room(room)
    user_id = room
    query_id = str(uuid.uuid4())
    # Get or create the per-user streaming state
    state = user_streaming_states[user_id]
    # Stop any previous streaming
    state.stop_streaming.set()
    # Reset the stop_streaming Event
    state.stop_streaming.clear()
    # Set the current query id
    state.current_query_id = query_id
    emit('clear_response', room=room)
    # Run stream_answer in a new thread
    threading.Thread(target=stream_answer, args=(query, assistant, app.config['DATA_NAME'], room, query_id, state)).start()

@socketio.on('stop_generating')
def handle_stop_generating():
    user_id = session.get('user_id')
    state = user_streaming_states.get(user_id)
    if state:
        state.stop_streaming.set()
    socketio.emit('stream_complete', room=user_id)

@socketio.on('connect')
def handle_connect():
    session_id = session.get('user_id')
    join_room(session_id)
    socketio.emit('session', {'session_id': session_id}, room=request.sid)

if __name__ == '__main__':
    socketio.run(app)

import openai
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room
import re
import uuid
from .config import Config
import threading
from collections import defaultdict

app = Flask(__name__)
app.config.from_object(Config)
socketio = SocketIO(app)
openai.api_key = app.config['OPENAI_API_KEY']

# Class to maintain per-user streaming state
class StreamingState:
    def __init__(self):
        self.stop_streaming = threading.Event()
        self.current_query_id = None

# Dictionary to store streaming states per user
user_streaming_states = defaultdict(StreamingState)

def stream_answer(query, room, query_id, state):
    try:
        response = openai.ChatCompletion.create(
            model=app.config['MODEL'],
            messages=[{"role": "user", "content": query}],
            stream=True,
        )
        for chunk in response:
            if state.stop_streaming.is_set() or state.current_query_id != query_id:
                break
            delta = chunk.choices[0].delta.get('content', '')
            if delta:
                clean_delta = clean_response(delta)
                socketio.emit('response', {'data': clean_delta}, room=room)
    except Exception as e:
        print(f"Error in stream_answer: {e}")
    finally:
        socketio.emit('stream_complete', room=room)

def clean_response(response):
    response = re.sub(r'【\d+:\d+†source】', '', response)
    response = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', response)
    response = response.replace('**', '')
    response = response.replace('\n', '<br>')
    return response

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
    if state.current_query_id:
        state.stop_streaming.set()
    # Reset the stop_streaming Event
    state.stop_streaming.clear()
    # Set the current query id
    state.current_query_id = query_id
    emit('clear_response', room=room)
    # Run stream_answer in a new thread
    threading.Thread(target=stream_answer, args=(query, room, query_id, state)).start()

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

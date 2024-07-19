from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit
from openai import OpenAI
from typing_extensions import override
from openai import AssistantEventHandler
import re
import uuid
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)
client = OpenAI()

active_threads = {}

class EventHandler(AssistantEventHandler):
    def __init__(self, session_id):
        self.session_id = session_id
        self.stop_flag = threading.Event()

    @override
    def on_text_created(self, text) -> None:
        pass

    @override
    def on_text_delta(self, delta, snapshot):
        if not self.stop_flag.is_set():
            clean_delta = clean_response(delta.value)
            socketio.emit('response', {'data': clean_delta}, room=self.session_id)

    def on_tool_call_created(self, tool_call):
        pass

    def on_tool_call_delta(self, delta, snapshot):
        if not self.stop_flag.is_set():
            if delta.type == 'code_interpreter':
                if delta.code_interpreter.input:
                    clean_input = clean_response(delta.code_interpreter.input)
                    socketio.emit('response', {'data': clean_input}, room=self.session_id)
                if delta.code_interpreter.outputs:
                    socketio.emit('response', {'data': "<br><br>output >"}, room=self.session_id)
                    for output in delta.code_interpreter.outputs:
                        if output.type == "logs":
                            clean_logs = clean_response(output.logs)
                            socketio.emit('response', {'data': clean_logs}, room=self.session_id)

def create_assistant(instructions, data_name, assistant_name, model='gpt-3.5-turbo'):
    vector_store_id = "vs_PILR6EF6tb1gCv4Hn3z7ylRV"
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

def stream_answer(query, assistant, data_name, session_id):
    vector_store_id = "vs_PILR6EF6tb1gCv4Hn3z7ylRV"
    thread = client.beta.threads.create(
        messages=[{"role": "user", "content": query, "attachments": []}],
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
    )

    event_handler = EventHandler(session_id)
    active_threads[session_id] = event_handler

    with client.beta.threads.runs.stream(
        thread_id=thread.id,
        assistant_id=assistant.id,
        instructions=assistant.instructions,
        event_handler=event_handler,
    ) as stream:
        stream.until_done()

    del active_threads[session_id]
    socketio.emit('generation_complete', room=session_id)

def clean_response(response):
    response = re.sub(r'【\d+:\d+†source】', '', response)
    response = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', response)
    response = response.replace('**', '')
    response = response.replace('\n', '<br>')
    return response

instructions = """
You are an assistant that points users to which individuals have similar research interests using the information provided to you.
When a user states their interests, you must provide a summary of the individuals that most resemble the user's interests and 
explain what in particular they have done or are doing that is similar to the user's interests.
Please use newlines to separate every single one of your sentences.

You must format your response as follows:
1. Provide the name, affiliation, and a brief summary of the relevant individuals'.
2. For each individual, elaborate on their specific research interests and provide their url, email, and address.

It is absolutely essential that you only use information from what is provided to you. Do not use any external information.
It is critical that all information is accurate and relevant to the user's interests.
It must be clear how the individuals' research interests are similar to the user's interests.

If no relevant individuals are found, please say so and provide a description of those working in the most similar areas.

Your response must be in regular english; do not use html.
Do not provide a general introduction or summary at the end.
Include two newlines only when changing to a new individual.
Responses must be brief. No more than  3 sentences per individual.
Use bullet points (•) when listing information, but not otherwise. Do not use bullet points for the brief summaries.

Response template:
1. [name, very brief title and university]
[~3 sentence summary of research interests as they relate to the user's interests]
• Email: [email]
• Website: [url]
• Address: [address]

If for example there is no website:
1. [name, very brief title and university]
[~3 sentence summary of research interests as they relate to the user's interests]
• Email: [email]
• Website: Not found.
• Address: [address]

Do not provide citations or sources.
URLs should be in the format of "https://www.example.com", no html formatting.
Do not tell people why research areas are relevant, unless it is highly non-obvious. They will know why they are relevant.
No boilerplate "his work pertains to area X", or "which relates to X". Just say what they do.
Abbreviate University of California to UC.
Remember to absolutely never provide citations or sources.
"""
data_name = 'pi_profiles'
assistant_name = 'pi_profiles_assistant'
assistant = create_assistant(instructions, data_name, assistant_name)

@app.route('/')
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
    return render_template('index.html')

@socketio.on('query')
def handle_query(query):
    session_id = request.sid
    emit('start_query', {'query': query}, room=session_id)
    threading.Thread(target=stream_answer, args=(query, assistant, data_name, session_id)).start()

@socketio.on('stop_generation')
def stop_generation():
    session_id = request.sid
    if session_id in active_threads:
        active_threads[session_id].stop_flag.set()
        del active_threads[session_id]
        emit('generation_stopped', room=session_id)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0')
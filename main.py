import os
import time
import threading
import random
import logging
from datetime import datetime
import asyncio
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
import openai
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from secrets import token_hex

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY_HERE")
openai.api_key = OPENAI_API_KEY

app = Flask(__name__)
app.secret_key = token_hex(16)

agents = {}
log_messages = []

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

def add_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    log_messages.append(entry)
    logger.info(message)

def get_session_id():
    if 'session_id' not in session:
        session['session_id'] = token_hex(16)
    return session['session_id']

class Agent:
    def __init__(self, telegram_token, character_desc, chat_id, min_interval, max_interval):
        self.telegram_token = telegram_token
        self.character_desc = character_desc
        self.chat_id = chat_id
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.running = False
        self.delete_permission = False
        self.application = None
        self.poster_thread = None

    def start(self):
        if not self.running:
            self.running = True
            if not self.application:
                self.application = start_telegram_bot(self)
            if not self.poster_thread or not self.poster_thread.is_alive():
                self.poster_thread = threading.Thread(target=periodic_poster, args=(self,), daemon=True)
                self.poster_thread.start()
            add_log("Agent started.")

    def stop(self):
        if self.running:
            self.running = False
            add_log("Agent stopped.")

    def delete(self):
        self.stop()
        if self.application:
            self.application = None
        add_log("Agent deleted.")

def get_character_response(agent, user_message="Generate the next message as this character."):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": agent.character_desc},
                {"role": "user", "content": user_message}
            ],
            temperature=0.9,
            max_tokens=200
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        add_log(f"Error calling OpenAI: {e}")
        return "*(The character seems speechless right now...)*"

def periodic_poster(agent):
    while agent.running:
        if agent.chat_id:
            interval = random.randint(agent.min_interval, agent.max_interval)
            add_log(f"Waiting {interval} minutes until next post...")
            time.sleep(interval * 60)

            # Ensure the event loop is available before sending a message
            loop = None
            while agent.running and loop is None:
                loop = agent.application.user_data.get("loop")
                if loop is None:
                    add_log("No event loop yet, waiting 5s...")
                    time.sleep(5)

            if not agent.running:
                break  # Agent stopped while waiting

            message = get_character_response(agent)
            async def send_msg():
                await agent.application.bot.send_message(
                    chat_id=agent.chat_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN
                )

            future = asyncio.run_coroutine_threadsafe(send_msg(), loop)
            future.result()
            add_log("Periodic message sent.")
        else:
            time.sleep(60)

INSULT_KEYWORDS = ["idiot", "stupid", "fool", "moron"]
FUD_KEYWORDS = ["scam", "fraud", "cheat", "fud"]
MARKETING_KEYWORDS = ["buy now", "limited offer", "discount", "promo"]

def is_negative_message(text: str) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in INSULT_KEYWORDS + FUD_KEYWORDS)

def is_marketing_message(text: str) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in MARKETING_KEYWORDS)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! I am your AI Agent.\nDo you grant me permission to delete insulting, negative or marketing messages?\nType /allowdeletion to grant permission."
    )
    add_log("/startagent command received in a group.")

async def allow_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agent = agents.get(get_session_id())
    if agent:
        agent.delete_permission = True
        await update.message.reply_text("Deletion permission granted.")
        add_log("Deletion permission granted by user.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agent = agents.get(get_session_id())
    if not agent:
        return
    if update.message is None:
        return
    text = update.message.text or ""
    bot_user = await context.bot.get_me()
    bot_mention = f"@{bot_user.username}"

    if bot_mention.lower() in text.lower():
        add_log(f"Bot mentioned: {text}")
        reply_text = get_character_response(agent, user_message=text)
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)

    if agent.delete_permission:
        if is_negative_message(text) or is_marketing_message(text):
            try:
                await update.message.delete()
                add_log(f"Deleted message: {text}")
            except Exception as e:
                add_log(f"Failed to delete message: {e}")

async def store_loop_job(context: ContextTypes.DEFAULT_TYPE):
    loop = asyncio.get_running_loop()
    context.application.user_data["loop"] = loop
    add_log("Event loop stored in application.user_data.")

async def post_init_callback(application):
    # Schedule a job at when=0 to store the loop once it is available
    application.job_queue.run_once(store_loop_job, when=0)

    # After scheduling the job, also send confirmation message if agent is running.
    # We'll do that after the loop is stored, so let's schedule another job for that too:
    application.job_queue.run_once(send_confirmation_job, when=1)

async def send_confirmation_job(context: ContextTypes.DEFAULT_TYPE):
    # Now that the event loop is available, send the confirmation message.
    for sid, agent in agents.items():
        if agent.running and agent.chat_id:
            intro_message = (
                "Agent is connected and running!\n\n"
                "How I Work:\n"
                "- Add me to a Telegram group.\n"
                "- In that group, type /startagent.\n"
                "- If allowed, I can delete negative/marketing messages.\n"
                "- Mention me to interact with my defined persona.\n"
                "- I will periodically post messages as configured.\n\n"
                "Manage my configuration from the web interface."
            )
            await context.application.bot.send_message(
                chat_id=agent.chat_id,
                text=intro_message,
                parse_mode=ParseMode.MARKDOWN
            )
            add_log("Sent confirmation message to the default chat.")
            break

def start_telegram_bot(agent):
    add_log("Starting Telegram bot polling...")
    application = ApplicationBuilder().token(agent.telegram_token).post_init(post_init_callback).build()
    application.add_handler(CommandHandler("startagent", start_command))
    application.add_handler(CommandHandler("allowdeletion", allow_deletion))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    threading.Thread(target=application.run_polling, daemon=True).start()
    return application

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>CYRUS TELEGRAM AI AGENT FACTORY</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
/* Same styling as before */
body {
    background: #0f0f0f;
    color: #00ff99;
    font-family: Consolas, monospace;
    margin: 0; padding: 0;
    display: flex;
    flex-direction: column;
    min-height: 100vh;
}
header {
    background: #0a0a0a;
    padding: 20px;
    text-align: center;
    border-bottom: 1px solid #00ff99;
}
h1 {
    font-size: 2em;
    margin: 0;
    color: #00ff99;
    text-shadow: 0 0 10px #00ff99;
    animation: glow 2s ease-in-out infinite alternate;
}
@keyframes glow {
    0% { text-shadow: 0 0 5px #00ff99; }
    100% { text-shadow: 0 0 20px #00ff99; }
}
main {
    display: flex;
    flex: 1;
    flex-wrap: wrap;
    padding: 20px;
    gap: 20px;
}
.card {
    background: #111;
    border: 1px solid #00ff99;
    border-radius: 8px;
    padding: 20px;
    flex: 1 1 300px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    animation: fadeIn 0.5s ease;
}
@keyframes fadeIn {
    from {opacity: 0;}
    to {opacity: 1;}
}
.card h2 {
    margin: 0;
    font-size: 1.2em;
    color: #00ff99;
    text-shadow: 0 0 5px #00ff99;
    margin-bottom: 10px;
}
.form-group {
    display: flex;
    flex-direction: column;
    gap: 5px;
}
.form-group label {
    font-size: 0.9em;
    color: #00ff99;
}
.form-input, textarea {
    background: #000000;
    color: #00ff99;
    border: 1px solid #00ff99;
    padding: 10px;
    border-radius: 4px;
    font-family: Consolas, monospace;
}
form input[type="submit"], button {
    background: #00ff99;
    color: #000;
    border: none;
    padding: 10px 15px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
    text-transform: uppercase;
    transition: transform 0.3s;
}
form input[type="submit"]:hover, button:hover {
    transform: translateY(-2px);
    box-shadow: 0 0 10px #00ff99;
}
.success {
    background: #003300;
    border-left: 5px solid #00ff99;
    padding: 10px;
}
.error {
    background: #330000;
    border-left: 5px solid #ff3333;
    padding: 10px;
    color: #ff9999;
}
.logs {
    background: #000;
    border: 1px solid #00ff99;
    border-radius: 4px;
    padding: 10px;
    height: 200px;
    overflow-y: auto;
    font-size: 0.8em;
}
.status-btns form {
    display: inline-block;
    margin-right: 5px;
}
.status-btns input[type="submit"] {
    font-size: 0.8em;
}
@media(max-width: 800px) {
    main {flex-direction: column;}
}
</style>
</head>
<body>
<header>
    <h1>CYRUS TELEGRAM AI AGENT FACTORY</h1>
</header>
<main>
    <div class="card">
        <h2>Configure Your AI Agent</h2>
        {% if message %}
        <div class="success">{{ message }}</div>
        {% endif %}
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="post" action="{{ url_for('index') }}">
            <div class="form-group">
                <label for="telegram_token">Telegram Bot Token:</label>
                <input type="text" id="telegram_token" name="telegram_token" class="form-input" placeholder="Your Bot Token" value="{{ telegram_token or '' }}" required>
            </div>
            <div class="form-group">
                <label for="character_desc">AI Character Description:</label>
                <textarea id="character_desc" name="character_desc" rows="4" class="form-input" placeholder="Describe the AI character persona..." required>{{ character_desc or '' }}</textarea>
            </div>
            <div class="form-group">
                <label for="chat_id">Default Chat ID (optional):</label>
                <input type="text" id="chat_id" name="chat_id" class="form-input" placeholder="@channelusername or ID" value="{{ chat_id or '' }}">
            </div>
            <div class="form-group">
                <label for="min_interval">Min Post Interval (minutes):</label>
                <input type="number" id="min_interval" name="min_interval" class="form-input" min="1" max="1440" value="{{ min_interval or '2' }}" required>
            </div>
            <div class="form-group">
                <label for="max_interval">Max Post Interval (minutes):</label>
                <input type="number" id="max_interval" name="max_interval" class="form-input" min="2" max="1440" value="{{ max_interval or '20' }}" required>
            </div>
            <input type="submit" value="Save & Start">
        </form>
    </div>

    <div class="card">
        <h2>Bot Logs</h2>
        <div class="logs" id="log"></div>
    </div>

    <div class="card">
        <h2>Agent Status</h2>
        <p>Status: {{ "Running" if running else "Stopped" }}</p>
        <div class="status-btns">
            {% if running %}
            <form method="post" action="{{ url_for('stop_agent') }}"><input type="submit" value="Stop"></form>
            {% else %}
            <form method="post" action="{{ url_for('start_agent') }}"><input type="submit" value="Start"></form>
            {% endif %}
            <form method="post" action="{{ url_for('delete_agent') }}"><input type="submit" value="Delete"></form>
        </div>
    </div>
</main>
<script>
function fetchLogs() {
    fetch('/logs')
    .then(response => response.json())
    .then(data => {
        const logDiv = document.getElementById('log');
        if (logDiv) {
            logDiv.innerHTML = '';
            data.forEach(entry => {
                const div = document.createElement('div');
                div.textContent = entry;
                logDiv.appendChild(div);
            });
        }
    })
    .catch(err => console.error('Error fetching logs:', err));
}
setInterval(fetchLogs, 5000);
fetchLogs();
</script>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    message = None
    error = None
    session_id = get_session_id()

    if request.method == "POST":
        telegram_token = request.form.get("telegram_token", "").strip()
        character_desc = request.form.get("character_desc", "").strip()
        chat_id = request.form.get("chat_id", "").strip()
        min_interval = int(request.form.get("min_interval", "2"))
        max_interval = int(request.form.get("max_interval", "20"))

        if telegram_token and character_desc:
            if session_id not in agents:
                agent = Agent(telegram_token, character_desc, chat_id, min_interval, max_interval)
                agents[session_id] = agent
                agent.start()
                message = "Configuration saved! Bot started."
            else:
                agent = agents[session_id]
                agent.telegram_token = telegram_token
                agent.character_desc = character_desc
                agent.chat_id = chat_id
                agent.min_interval = min_interval
                agent.max_interval = max_interval
                add_log("Agent configuration updated.")
                if agent.running:
                    agent.stop()
                    agent.start()
                message = "Configuration updated!"
        else:
            error = "Please provide a Telegram Bot Token and Character Description."

    agent = agents.get(session_id)
    running = agent.running if agent else False

    return render_template_string(
        HTML_TEMPLATE,
        telegram_token=agent.telegram_token if agent else None,
        character_desc=agent.character_desc if agent else None,
        chat_id=agent.chat_id if agent else None,
        min_interval=agent.min_interval if agent else 2,
        max_interval=agent.max_interval if agent else 20,
        running=running,
        message=message,
        error=error
    )

@app.route("/start", methods=["POST"])
def start_agent():
    agent = agents.get(get_session_id())
    if agent and not agent.running:
        agent.start()
    return redirect(url_for('index'))

@app.route("/stop", methods=["POST"])
def stop_agent():
    agent = agents.get(get_session_id())
    if agent and agent.running:
        agent.stop()
    return redirect(url_for('index'))

@app.route("/delete", methods=["POST"])
def delete_agent():
    session_id = get_session_id()
    if session_id in agents:
        agents[session_id].delete()
        del agents[session_id]
    return redirect(url_for('index'))

@app.route("/logs")
def get_logs():
    return jsonify(log_messages[-200:])

if __name__ == "__main__":
    add_log("Starting Flask server...")
    app.run(host="0.0.0.0", port=5000, debug=True)

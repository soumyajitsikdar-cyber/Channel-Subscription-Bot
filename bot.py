# Improved Telegram Subscription Bot (Production Ready)

import os
import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# --- KEEP ALIVE SERVER ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"


def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    Thread(target=run_web).start()

# --- ENV VARIABLES ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
UPI_ID = os.getenv('UPI_ID')
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
client = MongoClient(MONGO_URI)
db = client['sub_management']
channels_col = db['channels']
users_col = db['users']

# INDEXES
users_col.create_index([("expiry", 1)])
users_col.create_index([("user_id", 1), ("channel_id", 1)])

# --- UTIL ---

def format_time(mins):
    mins = int(mins)
    if mins < 60:
        return f"{mins} Min"
    elif mins < 1440:
        return f"{mins//60} Hours"
    else:
        return f"{mins//1440} Days"

# --- START ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) > 1:
        try:
            ch_id = int(args[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})
            if not ch_data:
                return

            markup = InlineKeyboardMarkup()
            for mins, price in ch_data['plans'].items():
                markup.add(InlineKeyboardButton(
                    f"💳 {format_time(mins)} - ₹{price}",
                    callback_data=f"select_{ch_id}_{mins}"
                ))

            markup.add(InlineKeyboardButton("📞 Contact", url=f"https://t.me/{CONTACT_USERNAME}"))

            bot.send_message(
                message.chat.id,
                f"*{ch_data['name']}*\n\nSelect a plan:",
                reply_markup=markup
            )
            return
        except:
            pass

    if user_id == ADMIN_ID:
        bot.send_message(user_id, "/add - Add Channel\n/channels - Manage")
    else:
        bot.send_message(user_id, "Use invite link to join channels.")

# --- ADD CHANNEL ---

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel(message):
    msg = bot.send_message(ADMIN_ID, "Forward a message from your channel.")
    bot.register_next_step_handler(msg, get_channel)


def get_channel(message):
    if not message.forward_from_chat:
        bot.send_message(ADMIN_ID, "Invalid forward. Try again.")
        return

    ch = message.forward_from_chat
    msg = bot.send_message(ADMIN_ID, "Send plans: 60:10, 1440:50")
    bot.register_next_step_handler(msg, save_channel, ch.id, ch.title)


def save_channel(message, ch_id, ch_name):
    try:
        plans = {}
        for item in message.text.split(','):
            t, p = item.strip().split(':')
            plans[t] = int(p)

        channels_col.update_one(
            {"channel_id": ch_id},
            {"$set": {"name": ch_name, "plans": plans, "admin_id": ADMIN_ID}},
            upsert=True
        )

        link = f"https://t.me/{bot.get_me().username}?start={ch_id}"
        bot.send_message(ADMIN_ID, f"Channel Added!\n{link}")

    except Exception as e:
        bot.send_message(ADMIN_ID, f"Error: {e}")

# --- PAYMENT ---

@bot.callback_query_handler(func=lambda c: c.data.startswith('select_'))
def select_plan(call):
    _, ch_id, mins = call.data.split('_')
    ch_data = channels_col.find_one({"channel_id": int(ch_id)})

    if not ch_data:
        return

    price = ch_data['plans'][mins]

    qr = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa={UPI_ID}&am={price}&cu=INR"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("I Paid", callback_data=f"paid_{ch_id}_{mins}"))

    bot.send_photo(
        call.message.chat.id,
        qr,
        caption=f"Pay ₹{price}\nPlan: {format_time(mins)}",
        reply_markup=markup
    )

# --- ADMIN APPROVAL ---

@bot.callback_query_handler(func=lambda c: c.data.startswith('paid_'))
def paid(call):
    _, ch_id, mins = call.data.split('_')
    user = call.from_user

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Approve", callback_data=f"app_{user.id}_{ch_id}_{mins}"))

    bot.send_message(
        ADMIN_ID,
        f"User {user.id} wants access\nChannel: {ch_id}\nPlan: {mins}",
        reply_markup=markup
    )

    bot.send_message(user.id, "Waiting for approval")

# --- APPROVE ---

@bot.callback_query_handler(func=lambda c: c.data.startswith('app_'))
def approve(call):
    _, u_id, ch_id, mins = call.data.split('_')
    u_id, ch_id, mins = int(u_id), int(ch_id), int(mins)

    existing = users_col.find_one({"user_id": u_id, "channel_id": ch_id})

    if existing and existing.get('expiry', 0) > time.time():
        expiry = datetime.fromtimestamp(existing['expiry']) + timedelta(minutes=mins)
    else:
        expiry = datetime.now() + timedelta(minutes=mins)

    expiry_ts = int(expiry.timestamp())

    link = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)

    users_col.update_one(
        {"user_id": u_id, "channel_id": ch_id},
        {"$set": {"expiry": expiry_ts}},
        upsert=True
    )

    bot.send_message(u_id, f"Approved! Join: {link.invite_link}")
    bot.edit_message_text("Approved", call.message.chat.id, call.message.message_id)

# --- AUTO REMOVE ---

def remove_expired():
    now = int(time.time())
    users = users_col.find({"expiry": {"$lte": now}})

    for u in users:
        try:
            bot.ban_chat_member(u['channel_id'], u['user_id'])
            bot.unban_chat_member(u['channel_id'], u['user_id'])
            users_col.delete_one({"_id": u['_id']})
        except:
            pass

# --- START ---

if __name__ == '__main__':
    keep_alive()

    scheduler = BackgroundScheduler()
    scheduler.add_job(remove_expired, 'interval', minutes=1)
    scheduler.start()

    bot.remove_webhook()
    print("Bot Running...")
    bot.infinity_polling()

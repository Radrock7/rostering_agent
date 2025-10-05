import pandas as pd
import requests
from io import StringIO
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import logging

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration - Load from environment variables
import os
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CSV_URL = os.getenv("CSV_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def fetch_csv_from_url(url):
    """Fetch CSV content from URL without storing the file."""
    response = requests.get(url)
    response.raise_for_status()
    return StringIO(response.text)

def extract_date_info(df, col_idx):
    """Extract date and day information from a column."""
    date = col_idx - 2
    day = df.iloc[2, col_idx]
    return date, day

def prepare_roster_data(df, target_date_col):
    """Extract relevant roster data for the target date."""
    roster_df = df.iloc[5:18, [1, target_date_col]].copy()
    roster_df.columns = ['Name', 'Duty']
    return roster_df

def generate_parade_state_with_gemini(api_key, roster_df, date, day):
    """Use Gemini API to generate parade state message."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash', generation_config=genai.GenerationConfig(
            temperature=0,
        ))
    
    prompt = f"""You are a military administrative assistant helping to generate a daily parade state message for a medical unit.

**INPUT DATA:**
Date: {date} OCTOBER 2025
Day: {day}

Roster (Name and Duty Assignment):
{roster_df.to_string(index=False)}

**DUTY CODES EXPLANATION:**
- M1, M2, M3, M4: Medic duty assignments
- MA: Medical Appointment
- DO: Duty Off
- OIL: Off In Lieu
- OSL: Off Sick Leave
- OFF: Official Off
- COURSE: Attending Course
- MC: Medical Certificate (Sick)
- NaN: No specific duty (Additional personnel)
- CPC: External staff (not counted in attendance)

**SPECIAL RULES:**
1. CPC is external staff and should NOT be counted in holding strength, present strength, or medic strength
2. If CPC has a duty (e.g., M2), list them as "M2: CPC" but don't count them in attendance
3. Anyone with NaN (no duty) should be listed under "Additional"
4. Anyone with MA, DO, OIL, OFF, COURSE, MC, OSL should be listed under "Medics:" section with their absence reason
5. Sort all names by military rank (highest to lowest): CPT > LTA > ME3 > ME2 > ME1 > 2SG > 3SG > CFC > CPL > LCP > PTE
6. Total holding strength is 17 (12 medics + 1 supply assistant + 2 MO + 2 SM)
7. Total medics = 12 (fixed)
8. There can be two medics doing the same duty but at different times (AM/PM), put their names together separated by "/" under the M1, M2, M3, M4 sections (e.g. M1: SGT TAN / CPL LEE). Put the AM duty name first, then PM duty.

**CALCULATIONS:**
- Present Strength: Holding strength minus those who are absent (MA, DO, OIL, OFF, COURSE, MC, OSL)
- Medic Strength: Total medics minus absent medics

**OUTPUT FORMAT:**
Generate EXACTLY this format:

PARADE STATE FOR {date} OCTOBER 2025 {day}

Holding Strength: 17
Present Strength: [calculate]/[holding strength]
Medic Strength: [calculate]/12

MO: 
CPT (DR) CHONG YUAN KAI:
CPT (DR) ANDRE WONG JUN HUI: 


SM:
ME3 KARRIE YAP:
ME2 BRYAN LIM:


Medics: 
[List absent personnel with reasons, sorted by rank]
[Format: RANK NAME: REASON]


M1: [Name(s) of M1 duty medic]
M2: [Name(s) of M2 duty medic]
M3: [Name(s) of M3 duty medic]
M4: [Name(s) of M4 duty medic]


C1: 
C5: 

Additional:
[List personnel with NaN duty, sorted by rank]
[One name per line]


BASE E (CPC): TBC
SUPPLY ASSISTANT
CFC HOVAN TAN: 

Flying Hours: TBC

**IMPORTANT:**
- Leave MO, SM, C1, C5, BASE E, SUPPLY ASSISTANT, and Flying Hours sections exactly as shown
- Only update the Medics, M1-M4, and Additional sections based on the roster data
- Ensure all names are sorted by rank within each section
- Do not add any extra commentary or explanation, just output the parade state message
"""
    
    response = model.generate_content(prompt)
    return response.text

def process_roster_with_gemini(url, api_key, target_date_col):
    """Process roster and generate parade state message using Gemini."""
    csv_data = fetch_csv_from_url(url)
    df = pd.read_csv(csv_data, header=None)
    date, day = extract_date_info(df, target_date_col)
    roster_df = prepare_roster_data(df, target_date_col)
    parade_state = generate_parade_state_with_gemini(api_key, roster_df, date, day)
    return parade_state

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = (
        "Welcome to the Parade State Generator Bot! 🎖️\n\n"
        "To generate a parade state, simply send me a date number (1-31).\n\n"
        "For example:\n"
        "• Send '5' for October 5th\n"
        "• Send '15' for October 15th\n\n"
        "You can also use these commands:\n"
        "/start - Show this welcome message\n"
        "/help - Show usage instructions"
    )
    await update.message.reply_text(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    help_message = (
        "📋 How to use this bot:\n\n"
        "1. Send a date number (1-31) to generate the parade state\n"
        "2. Wait for the bot to process the roster\n"
        "3. Receive your formatted parade state message\n\n"
        "Example: Send '12' to get the parade state for October 12th\n\n"
        "Need help? Contact your administrator."
    )
    await update.message.reply_text(help_message)

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date input from user."""
    try:
        user_input = update.message.text.strip()
        date = int(user_input)
        
        if date < 1 or date > 31:
            await update.message.reply_text(
                "❌ Invalid date. Please enter a number between 1 and 31."
            )
            return
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            f"⏳ Generating parade state for October {date}...\n"
            "Please wait a moment."
        )
        
        # Calculate column index
        target_column = date + 2
        
        # Generate parade state
        parade_state = process_roster_with_gemini(CSV_URL, GEMINI_API_KEY, target_column)
        
        # Send the result
        await processing_msg.edit_text(
            f"✅ Parade state generated successfully!\n\n{parade_state}"
        )
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid input. Please send a valid date number (1-31)."
        )
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        await update.message.reply_text(
            "❌ An error occurred while generating the parade state.\n"
            "Please try again or contact the administrator."
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date))
    
    # Register error handler
    application.add_error_handler(error_handler)
    
    # Start the Bot
    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
import pandas as pd
import requests
from io import StringIO
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
import logging
import os

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration - Load from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEETS_API_KEY = os.getenv("GOOGLE_SHEETS_API_KEY")
MAIN_SPREADSHEET_ID = os.getenv("MAIN_SPREADSHEET_ID")
C1_C5_SPREADSHEET_ID = os.getenv("C1_C5_SPREADSHEET_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEDICS_REFERENCE_LIST = os.getenv("MEDICS_REFERENCE_LIST")  # Reference list of medics with full names and ranks

# Conversation states
SELECTING_DATE, SELECTING_MAIN_SHEET, SELECTING_C1C5_SHEET = range(3)

def get_sheet_names(spreadsheet_id, api_key):
    """Fetch all sheet names and IDs from a Google Spreadsheet."""
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?key={api_key}'
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        sheets = []
        for sheet in data['sheets']:
            sheets.append({
                'name': sheet['properties']['title'],
                'id': sheet['properties']['sheetId']
            })
        return sheets
    else:
        raise Exception(f"Error fetching sheets: {response.status_code}")

def build_csv_url(spreadsheet_id, sheet_id):
    """Build CSV export URL from spreadsheet ID and sheet ID."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={sheet_id}"

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
    month = df.iloc[1, 1]
    return roster_df, month

def generate_parade_state_with_gemini(api_key, roster_df, date, day, month):
    """Use Gemini API to generate parade state message."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash', generation_config=genai.GenerationConfig(
            temperature=0,
        ))
    
    prompt = f"""You are a military administrative assistant helping to generate a daily parade state message for a medical unit.

**INPUT DATA:**
Date: {date} {month} 2025
Day: {day} (Spell out full word in caps)

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
2. If CPC has a duty (e.g., M2), another person will be assigned as M-2. In the parade state, list them as "M2: <M-2 Rank and Name>/ CPC". Do not count CPC in strength calculations.
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

PARADE STATE FOR {date} {month} 2025 {day}

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
DAY: PAO TBC (SBAB), PAO TBC (CPC)
NIGHT: PAO TBC (SBAB), PAO TBC (CPC)

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

def prepare_c1_c5_data(df, target_date_col):
    """Extract C1/C5 duty data for previous and current day."""
    target_data = df.iloc[5:21, [1, target_date_col - 1, target_date_col]].copy().reset_index(drop=True)
    target_data.columns = ['Name', 'Previous Day Duty', 'Current Day Duty']
    return target_data

def fill_c1_c5_with_gemini(api_key, parade_state, c1_c5_df):
    """Use Gemini API to fill in C1 and C5 personnel in parade state."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash', generation_config=genai.GenerationConfig(
            temperature=0,
        ))
    
    prompt = f"""You are an administrative assistant in charge of identifying the C1 and C5 personnel for a military unit based on a duty roster.

Look at the following duty roster (previous day and current day) and identify the C1 and C5 personnel for the current day (second column). If it is not stated explicitly, then the previous day C1 will be the current day C5.

ROSTER:
{c1_c5_df.to_string(index=False)}

Fill in the C1 and C5 personnel rank and name in the parade state message below. Just output the completed message without any extra commentary or explanation.

**PARADE STATE MESSAGE:**
{parade_state}
"""
    
    response = model.generate_content(prompt)
    return response.text

def correct_medic_names_ranks(api_key, parade_state, reference_list):
    """Use Gemini API to correct medic names and ranks in parade state."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash', generation_config=genai.GenerationConfig(
            temperature=0,
        ))
    
    prompt = f"""You are an administrative assistant in charge of verifying and correcting medic names and ranks in a military parade state message.

**REFERENCE LIST OF MEDICS (Correct Full Names and Updated Ranks):**
{reference_list}

**YOUR TASK:**
1. Review the "Medics:" section, "M1-M4" sections, and "Additional:" section in the parade state below
2. Match the names in the parade state with the reference list above
3. Correct any discrepancies in:
   - Rank (ensure it matches the reference list)
   - Name (ensure full names are used as per reference list)
4. Preserve the exact format and structure of the parade state
5. Do NOT modify any other sections (MO, SM, C1, C5, BASE E, SUPPLY ASSISTANT, Flying Hours)
6. Only correct medic-related entries

**PARADE STATE MESSAGE:**
{parade_state}

Output the corrected parade state message with accurate medic names and ranks. Do not add any extra commentary or explanation.
"""
    
    response = model.generate_content(prompt)
    return response.text

def process_full_parade_state(main_csv_url, c1_c5_csv_url, api_key, reference_list, target_date_col):
    """Generate complete parade state with C1, C5, and corrected medic names/ranks."""
    # Step 1: Generate initial parade state
    csv_data = fetch_csv_from_url(main_csv_url)
    df = pd.read_csv(csv_data, header=None)
    date, day = extract_date_info(df, target_date_col)
    roster_df, month = prepare_roster_data(df, target_date_col)
    initial_parade_state = generate_parade_state_with_gemini(api_key, roster_df, date, day, month)
    
    # Step 2: Fetch C1/C5 data and fill in C1 and C5
    csv_data = fetch_csv_from_url(c1_c5_csv_url)
    df = pd.read_csv(csv_data, header=None)
    c1_c5_df = prepare_c1_c5_data(df, target_date_col)
    parade_state_with_c1_c5 = fill_c1_c5_with_gemini(api_key, initial_parade_state, c1_c5_df)
    
    # Step 3: Correct medic names and ranks using reference list
    final_parade_state = correct_medic_names_ranks(api_key, parade_state_with_c1_c5, reference_list)
    
    return final_parade_state

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = (
        "Welcome to the Parade State Generator Bot! 🎖️\n\n"
        "To generate a parade state:\n"
        "1. Use /generate command\n"
        "2. Select the correct sheets from both spreadsheets\n"
        "3. Enter the date (1-31)\n"
        "4. Receive your parade state\n\n"
        "Commands:\n"
        "/generate - Start generating a parade state\n"
        "/help - Show usage instructions\n"
        "/cancel - Cancel current operation"
    )
    await update.message.reply_text(welcome_message)
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    help_message = (
        "📋 How to use this bot:\n\n"
        "1. Send /generate to start\n"
        "2. Select the main roster sheet\n"
        "3. Select the C1/C5 duty sheet\n"
        "4. Enter a date number (1-31)\n"
        "5. Wait for processing\n"
        "6. Receive your formatted parade state\n\n"
        "You can cancel anytime with /cancel\n\n"
        "Need help? Contact your administrator."
    )
    await update.message.reply_text(help_message)

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the parade state generation process."""
    try:
        # Fetch sheets from main spreadsheet
        sheets = get_sheet_names(MAIN_SPREADSHEET_ID, GOOGLE_SHEETS_API_KEY)
        
        # Create inline keyboard with sheet options
        keyboard = []
        for sheet in sheets:
            keyboard.append([InlineKeyboardButton(
                sheet['name'], 
                callback_data=f"main_{sheet['id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📊 Step 1/3: Select the MAIN ROSTER sheet:",
            reply_markup=reply_markup
        )
        
        return SELECTING_MAIN_SHEET
        
    except Exception as e:
        logger.error(f"Error fetching sheets: {e}")
        await update.message.reply_text(
            "❌ Error fetching spreadsheet data. Please check configuration."
        )
        return ConversationHandler.END

async def main_sheet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main sheet selection."""
    query = update.callback_query
    await query.answer()
    
    # Extract sheet ID from callback data
    sheet_id = query.data.replace("main_", "")
    context.user_data['main_sheet_id'] = sheet_id
    
    try:
        # Fetch sheets from C1/C5 spreadsheet
        sheets = get_sheet_names(C1_C5_SPREADSHEET_ID, GOOGLE_SHEETS_API_KEY)
        
        # Create inline keyboard with sheet options
        keyboard = []
        for sheet in sheets:
            keyboard.append([InlineKeyboardButton(
                sheet['name'], 
                callback_data=f"c1c5_{sheet['id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ Main roster sheet selected!\n\n"
            f"📊 Step 2/3: Select the C1/C5 DUTY sheet:",
            reply_markup=reply_markup
        )
        
        return SELECTING_C1C5_SHEET
        
    except Exception as e:
        logger.error(f"Error fetching C1/C5 sheets: {e}")
        await query.edit_message_text(
            "❌ Error fetching C1/C5 spreadsheet data. Please try again."
        )
        return ConversationHandler.END

async def c1c5_sheet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle C1/C5 sheet selection."""
    query = update.callback_query
    await query.answer()
    
    # Extract sheet ID from callback data
    sheet_id = query.data.replace("c1c5_", "")
    context.user_data['c1c5_sheet_id'] = sheet_id
    
    await query.edit_message_text(
        "✅ C1/C5 duty sheet selected!\n\n"
        "📅 Step 3/3: Enter the date (1-31) for the parade state:"
    )
    
    return SELECTING_DATE

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date input from user."""
    try:
        user_input = update.message.text.strip()
        date = int(user_input)
        
        if date < 1 or date > 31:
            await update.message.reply_text(
                "❌ Invalid date. Please enter a number between 1 and 31."
            )
            return SELECTING_DATE
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            f"⏳ Generating parade state for October {date}...\n"
            "Please wait a moment."
        )
        
        # Build CSV URLs
        main_sheet_id = context.user_data.get('main_sheet_id')
        c1c5_sheet_id = context.user_data.get('c1c5_sheet_id')
        
        main_csv_url = build_csv_url(MAIN_SPREADSHEET_ID, main_sheet_id)
        c1c5_csv_url = build_csv_url(C1_C5_SPREADSHEET_ID, c1c5_sheet_id)
        
        # Calculate column index
        target_column = date + 2
        
        # Generate complete parade state (with C1, C5, and corrected names/ranks)
        parade_state = process_full_parade_state(
            main_csv_url, 
            c1c5_csv_url, 
            GEMINI_API_KEY,
            MEDICS_REFERENCE_LIST,
            target_column
        )
        
        # Send the result
        await processing_msg.edit_text(
            f"✅ Parade state generated successfully!\n\n{parade_state}"
        )
        
        # Clear user data
        context.user_data.clear()
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid input. Please send a valid date number (1-31)."
        )
        return SELECTING_DATE
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        await update.message.reply_text(
            "❌ An error occurred while generating the parade state.\n"
            "Please try again with /generate or contact the administrator."
        )
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Operation cancelled. Use /generate to start again."
    )
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Create conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("generate", generate_command)],
        states={
            SELECTING_MAIN_SHEET: [CallbackQueryHandler(main_sheet_selected)],
            SELECTING_C1C5_SHEET: [CallbackQueryHandler(c1c5_sheet_selected)],
            SELECTING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    
    # Register error handler
    application.add_error_handler(error_handler)
    
    # Start the Bot
    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
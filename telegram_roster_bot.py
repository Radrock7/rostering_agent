import pandas as pd
import requests
from io import StringIO
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
import logging
import os
import gspread
from google.oauth2.service_account import Credentials
from typing import Optional, Tuple, Dict, List
import json
from dotenv import load_dotenv
load_dotenv()
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
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEDICS_REFERENCE_LIST = os.getenv("MEDICS_REFERENCE_LIST")  # Reference list of medics with full names and ranks
CURRENT_MONTH = os.getenv("CURRENT_MONTH", "OCTOBER")  # Default to OCTOBER

# Conversation states
SELECTING_DATE, SELECTING_MAIN_SHEET, SELECTING_C1C5_SHEET = range(3)

class LeaveTracker:
    """Track leave status from Google Sheets"""
    
    def __init__(self, spreadsheet_id: str, sheet_id: int, credentials_json: str = None):
        """Initialize Leave Tracker with service account credentials and specific sheet ID
        
        Args:
            spreadsheet_id: The Google Spreadsheet ID
            sheet_id: The specific sheet ID (gid) to open
            credentials_json: Service account credentials as JSON string
        """
        scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        
        if credentials_json:
            # Load from JSON string (environment variable)
            creds_json = json.loads(credentials_json)
            creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
        else:
            raise ValueError("No credentials provided. Set GOOGLE_CREDENTIALS_JSON env var")
        
        self.client = gspread.authorize(creds)
        spreadsheet = self.client.open_by_key(spreadsheet_id)
        
        # Open the specific sheet by ID
        try:
            self.sheet = spreadsheet.get_worksheet_by_id(sheet_id)
            logger.info(f"Opened sheet with ID: {sheet_id}, Title: {self.sheet.title}")
        except gspread.WorksheetNotFound:
            logger.error(f"Sheet with ID {sheet_id} not found. Using first sheet as fallback.")
            self.sheet = spreadsheet.get_worksheet(0)
        
        # Column and row configuration
        self.NAME_COLUMN = 2  # Column B
        self.DAY_1_COLUMN = 4  # Column D
        self.START_ROW = 6     # First person's row
        self.END_ROW = 17      # Last person's row
        
        # Leave types to detect
        self.LEAVE_TYPES = ['MC', 'LL', 'OSL', 'COMPASSIONATE LEAVE', 'COURSE']
        
        # Cache merged ranges for the specific sheet
        metadata = self.sheet.spreadsheet.fetch_sheet_metadata()
        # Find the merges for this specific sheet
        for sheet_metadata in metadata['sheets']:
            if sheet_metadata['properties']['sheetId'] == sheet_id:
                self.merged_ranges = sheet_metadata.get('merges', [])
                logger.info(f"Found {len(self.merged_ranges)} merged ranges in sheet")
                break
        else:
            self.merged_ranges = []
            logger.warning(f"No merged ranges found for sheet ID {sheet_id}")
    
    def _is_leave_cell(self, cell_value: str) -> bool:
        """Check if cell contains any leave type"""
        if not cell_value:
            return False
        cell_upper = cell_value.upper()
        return any(leave_type in cell_upper for leave_type in self.LEAVE_TYPES)
    
    def _find_person_row(self, name: str) -> Optional[int]:
        """Find the row number for a person by name"""
        for row in range(self.START_ROW, self.END_ROW + 1):
            cell_value = self.sheet.cell(row, self.NAME_COLUMN).value
            if cell_value and cell_value.strip() == name.strip():
                return row
        return None
    
    def check_leave_on_day(self, row_idx: int, day: int) -> Optional[Tuple[str, int, int]]:
        """
        Check if person is on leave for a specific day
        
        Args:
            row_idx: Row number of the person
            day: Day number (1-31)
            
        Returns:
            Tuple of (cell_value, start_day, end_day) if on leave, None otherwise
        """
        # Check all merged ranges for this row
        for merge in self.merged_ranges:
            start_row = merge['startRowIndex'] + 1
            end_row = merge['endRowIndex']
            start_col = merge['startColumnIndex'] + 1
            end_col = merge['endColumnIndex']
            
            # Check if this merge affects the person's row
            if start_row <= row_idx <= end_row:
                # Calculate day range
                start_day = start_col - (self.DAY_1_COLUMN - 1)
                end_day = end_col - (self.DAY_1_COLUMN - 1)
                
                # Check if the queried day falls within this range
                if start_day <= day <= end_day:
                    cell_value = self.sheet.cell(row_idx, start_col).value or ""
                    
                    if self._is_leave_cell(cell_value):
                        return (cell_value.strip(), start_day, end_day)
        
        return None
    
    def check_people_on_day(self, names: List[str], day: int) -> Dict[str, Optional[Tuple[str, int, int]]]:
        """
        Check leave status for specific people on a day
        
        Args:
            names: List of names to check
            day: Day number (1-31)
            
        Returns:
            Dictionary mapping names to their leave info (leave_type, start_day, end_day)
        """
        results = {}
        for name in names:
            row_idx = self._find_person_row(name)
            logger.info(f"Checking leave for '{name}' at row {row_idx}")
            if row_idx:
                results[name] = self.check_leave_on_day(row_idx, day)
                if results[name]:
                    logger.info(f"Leave detected for '{name}': {results[name]}")
            else:
                logger.warning(f"'{name}' not found in leave spreadsheet")
        
        return results

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
    return roster_df

def update_roster_with_leave_info(roster_df, date, month, spreadsheet_id, sheet_id, credentials_json):
    """
    Update roster with leave information for personnel with NaN duty
    
    Args:
        roster_df: DataFrame with Name and Duty columns
        date: Day number (1-31)
        month: Month name (e.g., "OCTOBER")
        spreadsheet_id: Google Sheets spreadsheet ID
        sheet_id: Specific sheet ID (gid) to check for leave
        credentials_json: Service account credentials JSON string
        
    Returns:
        Updated DataFrame with leave information
    """
    try:
        # Find people with NaN duty (no specific assignment)
        names_to_check = roster_df[roster_df['Duty'].isna()]['Name'].tolist()
        
        if not names_to_check:
            logger.info("No personnel with NaN duty to check for leave")
            return roster_df
        
        # Remove last entry if it exists (often a summary row)
        if names_to_check:
            names_to_check = names_to_check[:-1] if len(names_to_check) > 1 else names_to_check
        
        logger.info(f"Checking leave for {len(names_to_check)} personnel: {names_to_check}")
        
        # Initialize leave tracker with specific sheet ID
        tracker = LeaveTracker(spreadsheet_id, sheet_id, credentials_json)
        
        # Check leave status
        results = tracker.check_people_on_day(names_to_check, date)
        
        # Update roster with leave information
        on_leave = {name: result for name, result in results.items() if result is not None}
        
        if on_leave:
            logger.info(f"Found {len(on_leave)} personnel on leave")
            for name, result in on_leave.items():
                leave_reason, start_day, end_day = result
                leave_info = f"{leave_reason} ({start_day} {month} - {end_day} {month})"
                roster_df.loc[roster_df['Name'] == name, 'Duty'] = leave_info
                logger.info(f"Updated {name}: {leave_info}")
        else:
            logger.info("No personnel on leave")
        
        return roster_df
        
    except Exception as e:
        logger.error(f"Error checking leave status: {e}")
        logger.warning("Continuing without leave information")
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
- LL: Leave of any kind
- COMPASSIONATE LEAVE: Compassionate leave
- NaN: No specific duty (Additional personnel)
- CPC: External staff (not counted in attendance)
- Leave entries with duration format: LEAVE_TYPE (START_DATE - END_DATE) should be listed with the duration included

**SPECIAL RULES:**
1. CPC is external staff and should NOT be counted in holding strength, present strength, or medic strength
2. If CPC has a duty (e.g., M2), another person will be assigned as M-2. In the parade state, list them as "M2: <M-2 Rank and Name>/ CPC". Do not count CPC in strength calculations.
3. Anyone with NaN (no duty) should be listed under "Additional"
4. Anyone with MA, DO, OIL, OFF, COURSE, MC, OSL, LL, COMPASSIONATE LEAVE should be listed under "Medics:" section with their absence reason
5. If leave information includes duration (e.g., "MC (5 OCTOBER - 8 OCTOBER)"), include the full duration in the output
6. Sort all names by military rank (highest to lowest): CPT > LTA > ME3 > ME2 > ME1 > 2SG > 3SG > CFC > CPL > LCP > PTE
7. Total holding strength is 17 (12 medics + 1 supply assistant + 2 MO + 2 SM)
8. Total medics = 12 (fixed)
9. There can be two medics doing the same duty but at different times (AM/PM), put their names together separated by "/" under the M1, M2, M3, M4 sections (e.g. M1: SGT TAN / CPL LEE). Put the AM duty name first, then PM duty.

**CALCULATIONS:**
- Present Strength: Holding strength minus those who are absent (MA, DO, OIL, OFF, COURSE, MC, OSL, LL, COMPASSIONATE LEAVE)
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

def process_full_parade_state(main_csv_url, c1_c5_csv_url, api_key, reference_list, target_date_col, date, month, spreadsheet_id, sheet_id, credentials_json):
    """Generate complete parade state with leave info, C1, C5, and corrected medic names/ranks."""
    # Step 1: Fetch main roster and prepare initial data
    csv_data = fetch_csv_from_url(main_csv_url)
    df = pd.read_csv(csv_data, header=None)
    date_num, day = extract_date_info(df, target_date_col)
    roster_df = prepare_roster_data(df, target_date_col)
    
    # Step 2: Update roster with leave information (using same spreadsheet and selected sheet)
    roster_df = update_roster_with_leave_info(roster_df, date, month, spreadsheet_id, sheet_id, credentials_json)
    
    # Step 3: Generate initial parade state with updated roster
    initial_parade_state = generate_parade_state_with_gemini(api_key, roster_df, date_num, day)
    
    # Step 4: Fetch C1/C5 data and fill in C1 and C5
    csv_data = fetch_csv_from_url(c1_c5_csv_url)
    df = pd.read_csv(csv_data, header=None)
    c1_c5_df = prepare_c1_c5_data(df, target_date_col)
    parade_state_with_c1_c5 = fill_c1_c5_with_gemini(api_key, initial_parade_state, c1_c5_df)
    
    # Step 5: Correct medic names and ranks using reference list
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
            f"⏳ Generating parade state for {CURRENT_MONTH} {date}...\n"
            "Checking leave status and processing roster...\n"
            "Please wait a moment."
        )
        
        # Get selected sheet IDs
        main_sheet_id = context.user_data.get('main_sheet_id')
        c1c5_sheet_id = context.user_data.get('c1c5_sheet_id')
        
        # Build CSV URLs
        main_csv_url = build_csv_url(MAIN_SPREADSHEET_ID, main_sheet_id)
        c1c5_csv_url = build_csv_url(C1_C5_SPREADSHEET_ID, c1c5_sheet_id)
        
        # Calculate column index
        target_column = date + 2
        
        # Generate complete parade state (with leave info, C1, C5, and corrected names/ranks)
        # Use same spreadsheet and sheet as main roster for leave tracking
        parade_state = process_full_parade_state(
            main_csv_url, 
            c1c5_csv_url, 
            GEMINI_API_KEY,
            MEDICS_REFERENCE_LIST,
            target_column,
            date,
            CURRENT_MONTH,
            MAIN_SPREADSHEET_ID,  # Same spreadsheet as main roster
            int(main_sheet_id),   # Same sheet ID as main roster
            GOOGLE_CREDENTIALS_JSON
        )
        
        # Send the result
        await processing_msg.edit_text(
            f"{parade_state}"
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
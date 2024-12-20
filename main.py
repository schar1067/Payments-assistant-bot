import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
import json
from openai import OpenAI
from dotenv import load_dotenv
import sys
import pytz
from typing import Optional, Dict, Any

# Load environment variables from .env file
load_dotenv()

FIREBASE_CREDENTIALS_PATH = 'serviceAccount.json'

class DateHandler:
    """Helper class to handle date operations"""
    
    def __init__(self):
        self.tz = pytz.timezone('America/Bogota')  # Colombian timezone
        
    def get_current_date(self) -> datetime:
        """Get current date in Colombia timezone"""
        return datetime.now(self.tz)
    
    def parse_relative_date(self, date_str: str) -> Optional[datetime]:
        """Parse relative date strings like 'ayer', 'hoy', etc."""
        current = self.get_current_date()
        date_str = date_str.lower().strip()
        
        date_mappings = {
            'hoy': current,
            'ayer': current - timedelta(days=1),
            'anteayer': current - timedelta(days=2),
            'mañana': current + timedelta(days=1)
        }
        
        return date_mappings.get(date_str)
    
    def get_date_range(self, time_frame: str) -> tuple[datetime, datetime]:
        """Get start and end dates for a given time frame"""
        now = self.get_current_date()
        
        ranges = {
            'today': (
                datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz),
                datetime.combine(now.date(), datetime.max.time(), tzinfo=self.tz)
            ),
            'yesterday': (
                datetime.combine((now - timedelta(days=1)).date(), datetime.min.time(), tzinfo=self.tz),
                datetime.combine((now - timedelta(days=1)).date(), datetime.max.time(), tzinfo=self.tz)
            ),
            'week': (
                now - timedelta(days=7),
                now
            ),
            'month': (
                now - timedelta(days=30),
                now
            ),
            'year': (
                now - timedelta(days=365),
                now
            )
        }
        
        return ranges.get(time_frame, (now - timedelta(days=30), now))

class DatabaseHandler:
    """Handles all database operations"""
    
    def __init__(self):
        try:
            # Check if Firebase app is already initialized
            if not firebase_admin._apps:
                # Get credentials from environment variable
                firebase_creds_json = os.getenv('FIREBASE_CREDENTIALS')
                if not firebase_creds_json:
                    raise ValueError("FIREBASE_CREDENTIALS environment variable not set")
                
                # Parse the JSON string into a dictionary
                cred_dict = json.loads(firebase_creds_json)
                
                # Initialize Firebase Admin with credentials
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
            
            # Get Firestore database instance and store it as instance variable
            self.db = firestore.client()
            print("Firebase initialized successfully")
                
        except Exception as e:
            print(f"Error initializing Firebase: {e}")
            sys.exit(1)

class PaymentHandler:
    """Handles payment operations with improved date handling and query structure"""
    
    def __init__(self, db):
        self.db = db
        self.date_handler = DateHandler()

    def prepare_payment_data(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare payment data with proper date handling"""
        params = command['params']
        current_date = self.date_handler.get_current_date()
        
        # Handle date if specified in the command
        date_str = params.get('date')
        if date_str:
            parsed_date = self.date_handler.parse_relative_date(date_str)
            if parsed_date:
                current_date = parsed_date
        
        # Prepare payment data with all required fields
        payment_data = {
            'recipient': params['recipient'],
            'amount': params['amount'],
            'metadata': params.get('metadata', ''),
            'date': current_date.strftime('%Y-%m-%d'),
            'timestamp': current_date  # Store as datetime for proper querying
        }
        
        print(f"Prepared payment data: {json.dumps({**payment_data, 'timestamp': payment_data['timestamp'].isoformat()}, indent=2)}")
        return payment_data

    async def add_payment(self, user_id: str, command: Dict[str, Any]) -> str:
        """Add a new payment record"""
        try:
            payment_data = self.prepare_payment_data(command)
            
            # Add document to Firestore
            payment_ref = self.db.collection('users').document(str(user_id))\
                .collection('payments').document()
            
            payment_ref.set(payment_data)
            
            # Format response
            response = f"✅ Pago registrado:\n💰 {payment_data['amount']:,} COP a {payment_data['recipient']}"
            if payment_data.get('metadata'):
                response += f"\n📝 Concepto: {payment_data['metadata']}"
            if payment_data.get('date'):
                response += f"\n📅 Fecha: {payment_data['date']}"
            return response
            
        except Exception as e:
            print(f"Error adding payment: {e}")
            return "❌ Error al registrar el pago. Por favor intenta de nuevo."
        
    async def query_payments(self, user_id: str, params: Dict[str, Any]) -> str:
        """Query payments with improved query structure to avoid index issues"""
        try:
            base_query = self.db.collection('users').document(str(user_id)).collection('payments')
            
            # If we only have a recipient filter, no need for complex querying
            if 'recipient' in params and 'time_frame' not in params:
                query = base_query.where('recipient', '==', params['recipient'])\
                                .order_by('timestamp', direction=firestore.Query.DESCENDING)
                
            # If we only have a time frame filter, use simple date range
            elif 'time_frame' in params and 'recipient' not in params:
                start_date, end_date = self.date_handler.get_date_range(params['time_frame'])
                query = base_query.where('timestamp', '>=', start_date)\
                                .where('timestamp', '<=', end_date)\
                                .order_by('timestamp', direction=firestore.Query.DESCENDING)
                
            # If we have both filters, we need to handle them carefully
            elif 'time_frame' in params and 'recipient' in params:
                # First get the date-filtered results
                start_date, end_date = self.date_handler.get_date_range(params['time_frame'])
                query = base_query.where('timestamp', '>=', start_date)\
                                .where('timestamp', '<=', end_date)\
                                .order_by('timestamp', direction=firestore.Query.DESCENDING)
                
                # Then filter by recipient in memory
                payments = list(query.stream())
                payments = [p for p in payments if p.get('recipient') == params['recipient']]
                return self.format_payment_response(payments)
            
            # If no filters, just get recent payments
            else:
                query = base_query.order_by('timestamp', direction=firestore.Query.DESCENDING)\
                                .limit(50)  # Limit to recent payments
            
            # Execute query
            payments = list(query.stream())
            return self.format_payment_response(payments)
            
        except Exception as e:
            print(f"Error querying payments: {e}")
            if "The query requires an index" in str(e):
                index_url = str(e).split("You can create it here: ")[1]
                print(f"Please create the required index at: {index_url}")
                return ("❌ Se requiere crear un índice en Firebase para esta consulta. "
                       "Por favor, contacta al administrador del sistema.")
            return "❌ Error al consultar los pagos. Por favor intenta de nuevo."

    def format_payment_response(self, payments: list) -> str:
        """Format payment query results into a response message"""
        if not payments:
            return "No se encontraron pagos para los criterios especificados."
        
        response = "📊 Historial de pagos:\n\n"
        total = 0
        
        for payment in payments:
            p = payment.to_dict() if hasattr(payment, 'to_dict') else payment
            amount = p.get('amount', 0)
            recipient = p.get('recipient', 'No especificado')
            metadata = p.get('metadata', '')
            date = p.get('date', '')  # Get the date string instead of timestamp
            
            response += f"💰 {amount:,} COP a {recipient}"
            if metadata:
                response += f" ({metadata})"
            if date:  # Use the date string directly
                response += f" 📅 {date}"
            response += "\n"
            total += amount
        
        response += f"\n💰 Total: {total:,} COP"
        return response

class Bot:
    """Main bot class that handles all operations"""
    
    SYSTEM_PROMPT = """
    You are a Colombian business assistant. Convert user messages into structured commands.
    Available commands:
    1. add_payment: Register a payment to someone
    2. add_debt: Register a debt
    3. query_payments: Check payment history, supports time and recipient filters
    4. query_debts: Check pending debts

    IMPORTANT: 
    - For payments and debts, you MUST extract the reason or concept from the message and include it in the metadata field.
    - Look for words after "por", "para", or any description of why the payment/debt exists.
    - For dates, recognize words like "ayer", "hoy", "anteayer" and convert them to proper dates.

    Required fields for add_payment:
    - recipient: The person receiving the payment
    - amount: The amount in pesos (convert text numbers to digits)
    - metadata: REQUIRED - The reason for the payment (what it was for)
    - date: Optional - The date of the payment (will use relative dates like 'ayer', 'hoy', etc.)

    Required fields for add_debt:
    - debtor: The person involved in the debt
    - amount: The amount in pesos (convert text numbers to digits)
    - metadata: REQUIRED - The reason for the debt (what it was for)
    - date: Optional - The date of the debt (will use relative dates like 'ayer', 'hoy', etc.)

    For query_payments:
    - recipient: Optional - Filter by specific person
    - time_frame: Optional - "today", "yesterday", "week", "month", "year"
    Both filters can be combined.

    Examples:
    User: "Pagué 50 mil pesos a Juan ayer por el almuerzo"
    Output: {
        "command": "add_payment",
        "params": {
            "recipient": "Juan",
            "amount": 50000,
            "metadata": "almuerzo",
            "date": "ayer"
        }
    }

    User: "Dame los pagos a Simon de ayer"
    Output: {
        "command": "query_payments",
        "params": {
            "recipient": "Simon",
            "time_frame": "yesterday"
        }
    }
    """
    
    def __init__(self):
        # Check environment variables
        self.env_vars = self.check_required_env_vars()
        
        # Initialize handlers
        self.db_handler = DatabaseHandler()
        self.payment_handler = PaymentHandler(self.db_handler.db)
        
        # Initialize OpenAI
        try:
            self.openai_client = OpenAI(api_key=self.env_vars["OPENAI_API_KEY"])
        except Exception as e:
            print(f"Error initializing OpenAI client: {e}")
            sys.exit(1)
    
    def check_required_env_vars(self):
        """Check if all required environment variables are set"""
        required_vars = {
            "TELEGRAM_CHATBOT_TOKEN": os.getenv("TELEGRAM_CHATBOT_TOKEN"),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY")
        }
        
        missing_vars = [var for var, value in required_vars.items() if not value]
        
        if missing_vars:
            print("Error: The following required environment variables are missing:")
            for var in missing_vars:
                print(f"- {var}")
            print("\nPlease make sure these variables are set in your .env file:")
            print("TELEGRAM_CHATBOT_TOKEN=your_telegram_token_here")
            print("OPENAI_API_KEY=your_openai_api_key_here")
            sys.exit(1)
        
        return required_vars
    
    async def parse_message(self, text: str) -> dict:
        """Parse natural language into structured command using GPT-4"""
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": text}
                ],
                temperature=0
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"Error parsing message: {e}")
            return None
    
    async def handle_command(self, command: dict, user_id: str) -> str:
        """Execute the parsed command and return response"""
        if command["command"] == "add_payment":
            return await self.payment_handler.add_payment(user_id, command)
        elif command["command"] == "query_payments":
            return await self.payment_handler.query_payments(user_id, command['params'])
        else:
            return "❌ Comando no soportado"
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages"""
        try:
            command = await self.parse_message(update.message.text)
            if not command:
                await update.message.reply_text("❌ No pude entender el comando. Por favor intenta de nuevo.")
                return

            response = await self.handle_command(command, update.effective_user.id)
            await update.message.reply_text(response)
            
        except Exception as e:
            print(f"Error: {e}")
            await update.message.reply_text("❌ Ocurrió un error. Por favor intenta de nuevo.")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_text = """
🤖 ¡Hola! Soy tu asistente de negocios.

Puedes pedirme:
- Registrar pagos (incluso de días anteriores)
- Consultar pagos realizados
- Registrar deudas
- Consultar deudas pendientes

Ejemplos:
✍️ "Pagué 50 mil pesos a Juan ayer por el almuerzo"
📊 "Dame los pagos a Simon de esta semana"
💰 "Le debo 100 mil pesos a María por el mercado"
📝 "Qué deudas tengo pendientes"
        """
        await update.message.reply_text(welcome_text)
    
    def run(self):
        """Start the bot"""
        application = Application.builder().token(self.env_vars["TELEGRAM_CHATBOT_TOKEN"]).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Start the bot
        print("Bot started...")
        application.run_polling()

if __name__ == '__main__':
    bot = Bot()
    bot.run()
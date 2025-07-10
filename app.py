import os
from flask import Flask, request, render_template, redirect, url_for, flash, session
from openai import OpenAI
from dotenv import load_dotenv
import base64
import io
import logging # Import logging
import markdown2

# Configure logging
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

# --- Database Imports ---
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
# --- End Database Imports ---

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# --- Flask Configuration ---
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a_very_secret_key_for_your_golf_caddie_app_CHANGE_THIS") # IMPORTANT: Change this in production!
# --- End Flask Configuration ---

# --- Configure OpenAI API ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
AI_MODEL_NAME = "gpt-4o" # Or "gpt-4o-mini"
# --- End OpenAI API Configuration ---

# --- Database Configuration ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///golf_caddie.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Define Database Models ---
class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    # Store par for each hole as a comma-separated string, or a JSON field
    par_string = db.Column(db.String(200), nullable=False, default='3,4,5,3,4,5,3,4,5,3,4,5,3,4,5,3,4,5') # Default for 18 holes of par 4

    def __repr__(self):
        return f'<Course {self.name}>'

class Round(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    user_identifier = db.Column(db.String(255), nullable=False) # To link rounds to a user session
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    scores_string = db.Column(db.String(200)) # e.g., "4,5,3,..." for 18 holes
    course = db.relationship('Course', backref=db.backref('rounds', lazy=True))

    def get_scores(self):
        return [int(s) for s in self.scores_string.split(',')] if self.scores_string else []

    def calculate_total_score(self):
        return sum(self.get_scores())

    def calculate_score_to_par(self):
        course_pars = [int(p) for p in self.course.par_string.split(',')]
        round_scores = self.get_scores()
        
        if len(round_scores) != len(course_pars):
            # This handles cases where data might be incomplete or mismatched
            # For a production app, more robust error handling or data validation would be needed
            return None 
            
        score_to_par = sum(s - p for s, p in zip(round_scores, course_pars))
        return score_to_par

# Model for User's Club Yardages
class UserClubYardage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_identifier = db.Column(db.String(255), nullable=False, index=True) # Link to user session
    club_name = db.Column(db.String(50), nullable=False)
    yardage = db.Column(db.Integer, nullable=False)

    __table_args__ = (db.UniqueConstraint('user_identifier', 'club_name', name='_user_club_uc'),)

    def __repr__(self):
        return f'<UserClubYardage {self.user_identifier} - {self.club_name}: {self.yardage}>'

# Common clubs for display/input
COMMON_CLUBS = [
    "Driver", "3-Wood", "5-Wood", "Hybrid",
    "2-Iron", "3-Iron", "4-Iron", "5-Iron",
    "6-Iron", "7-Iron", "8-Iron", "9-Iron",
    "Pitching Wedge", "Gap Wedge", "Sand Wedge", "Lob Wedge",
    "Putter"
]

# --- End Database Models ---

# Helper to create tables if they don't exist
with app.app_context():
    db.create_all()

# Helper function to check allowed file extensions
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif'}

# Helper function to encode image to base64
def encode_image(image_input):
    """
    Encodes an image file or base64 data URL to a base64 string.
    Expects either a Werkzeug FileStorage object or a data URL string.
    """
    if isinstance(image_input, str) and image_input.startswith('data:image'):
        # It's a data URL from camera, extract base64 part
        return image_input.split(',')[1]
    elif hasattr(image_input, 'read'):
        # It's a file storage object from file input
        image_input.seek(0) # Ensure we read from the beginning
        return base64.b64encode(image_input.read()).decode('utf-8')
    return None

# Helper to generate AI response
def get_ai_response(prompt_text, image_base64=None, system_message=None):
    if system_message is None:
        system_message = "You are a helpful AI assistant." # Default system message

    messages = [{"role": "system", "content": system_message}]
    
    user_content = [{"type": "text", "text": prompt_text}]
    if image_base64:
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
    
    messages.append({"role": "user", "content": user_content})

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=messages,
            max_tokens=500, # Increased max_tokens for more comprehensive advice
        )
        return response.choices[0].message.content
    except Exception as e:
        app_logger.error(f"OpenAI API error: {e}")
        return f"<p class='error-message'>An error occurred while getting AI response: {e}</p>"


@app.route('/')
def index():
    # Set a simple user ID for the session if not already set
    if 'user_id' not in session:
        session['user_id'] = os.urandom(24).hex() # Generate a random hex string for user ID
    return render_template('index.html', current_page='home')

@app.route('/shot_advice')
def shot_advice_page():
    return render_template('shot_advice.html', current_page='shot_advice')

@app.route('/ask_caddie', methods=['POST'])
def ask_caddie():
    user_situation = request.form.get('situation', '').strip()
    target_yardage = request.form.get('yardage', '').strip() # Get yardage to target from form

    image_file = request.files.get('image_file')
    camera_image_data = request.form.get('camera_image_data')

    image_base64 = encode_image(image_file) if image_file else None
    if not image_base64 and camera_image_data:
        image_base64 = encode_image(camera_image_data)

    # Validate inputs
    if not user_situation and not image_base64 and not target_yardage:
        return "<p class='error-message'>Please provide a situation description, yardage, or an image for advice.</p>", 400

    prompt_parts = []
    
    # Fetch user's club yardages for context
    user_identifier = session.get('user_id')
    if user_identifier:
        user_yardages = UserClubYardage.query.filter_by(user_identifier=user_identifier).all()
        if user_yardages:
            yardage_info = ", ".join([f"{y.club_name}: {y.yardage} yards" for y in user_yardages])
            prompt_parts.append(f"My typical club yardages are as follows: {yardage_info}.")
        else:
            prompt_parts.append("I do not have my club yardages saved yet. Please provide advice based on typical amateur distances if suggesting clubs.")
    else:
        prompt_parts.append("User ID not found, cannot retrieve custom club yardages. Please provide advice based on typical amateur distances if suggesting clubs.")

    if user_situation:
        prompt_parts.append(f"The golf situation is: {user_situation}.")
    if target_yardage:
        prompt_parts.append(f"My current distance to the target is: {target_yardage} yards.")
    
    prompt = " ".join(prompt_parts) if prompt_parts else "Please provide golf shot advice."

    system_message = (
        "You are an expert golf caddie. Analyze the given golf situation (and image if provided) "
        "and offer concise, strategic, and helpful advice. Consider club selection, shot type, "
        "and general strategy. Be encouraging and clear. If yardages are provided (both target "
        "distance and user's club yardages), factor them into club selection. If no image is provided, "
        "rely solely on the text description. Assume a right-handed golfer unless specified otherwise."
        "Separate your output and organize it clearly so it's easily digestible"
    )

    ai_advice = get_ai_response(prompt, image_base64, system_message)
    return ai_advice

@app.route('/swing_analysis')
def swing_analysis_page():
    return render_template('swing_analysis.html', current_page='swing_analysis')

@app.route('/process_swing_analysis', methods=['POST'])
def process_swing_analysis():
    user_notes = request.form.get('notes', '').strip()
    image_file = request.files.get('image_file')
    camera_image_data = request.form.get('camera_image_data')

    image_base64 = encode_image(image_file) if image_file else None
    if not image_base64 and camera_image_data:
        image_base64 = encode_image(camera_image_data)

    if not image_base64:
        return "<p class='error-message'>Please provide an image for swing analysis.</p>", 400
    
    prompt = "Please analyze the golf swing in the image. Provide constructive feedback and tips for improvement. "
    if user_notes:
        prompt += f"The user noted: '{user_notes}'. Consider this in your analysis."
    prompt += "Assume a right-handed golfer unless the image clearly indicates otherwise."

    system_message = (
        "You are an expert golf swing coach. Analyze the provided image (and notes if provided) "
        "of a golf swing. Provide constructive feedback, identify potential areas for improvement, "
        "and suggest drills or adjustments. Be encouraging and clear."
    )

    ai_analysis = get_ai_response(prompt, image_base64, system_message)
    return ai_analysis

@app.route('/input_yardages', methods=['GET', 'POST'])
def input_yardages():
    user_identifier = session.get('user_id')
    if not user_identifier:
        flash("Please set a user ID (or log in) to input yardages.", "danger")
        return redirect(url_for('index'))

    if request.method == 'POST':
        for club in COMMON_CLUBS:
            club_input_name = club.replace('-', '_').lower().replace(' ', '_') # Handle hyphens and spaces
            yardage_str = request.form.get(club_input_name)
            
            if yardage_str and yardage_str.isdigit():
                yardage = int(yardage_str)
                if not (0 <= yardage <= 499): # Validate range
                    flash(f"Yardage for {club} must be between 0 and 499.", "warning")
                    continue # Skip this club but continue processing others

                existing_yardage = UserClubYardage.query.filter_by(user_identifier=user_identifier, club_name=club).first()
                if existing_yardage:
                    existing_yardage.yardage = yardage
                else:
                    new_yardage = UserClubYardage(user_identifier=user_identifier, club_name=club, yardage=yardage)
                    db.session.add(new_yardage)
            else:
                existing_yardage = UserClubYardage.query.filter_by(user_identifier=user_identifier, club_name=club).first()
                if existing_yardage:
                    db.session.delete(existing_yardage)
        
        try:
            db.session.commit()
            flash("Your club yardages have been saved!", "success")
        except Exception as e:
            db.session.rollback()
            app_logger.error(f"Error saving yardages: {e}")
            flash(f"An error occurred while saving yardages. Please try again.", "danger")
        
        return redirect(url_for('input_yardages')) # Redirect to GET to show updated data

    # For GET request, display current yardages
    current_yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_identifier).all()
    current_yardages = {y.club_name: y.yardage for y in current_yardage_objects}

    return render_template('input_yardages.html', 
                           clubs=COMMON_CLUBS, 
                           current_yardages=current_yardages,
                           current_page='input_yardages')

@app.route('/view_yardages')
def view_yardages():
    user_identifier = session.get('user_id')
    if not user_identifier:
        flash("Please set a user ID (or log in) to view yardages.", "danger")
        return redirect(url_for('index'))

    yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_identifier).order_by(UserClubYardage.club_name).all()
    
    # Sort clubs in a custom order for consistent display
    display_yardages = {}
    for club in COMMON_CLUBS:
        for obj in yardage_objects:
            if obj.club_name == club:
                display_yardages[club] = obj.yardage
                break # Found the club, move to next in COMMON_CLUBS
    
    # Filter out clubs that don't have a yardage (or were not in COMMON_CLUBS for some reason)
    # This ensures only clubs with data are passed, in the correct order
    final_display_yardages = {club: yardage for club, yardage in display_yardages.items() if yardage is not None}

    return render_template('view_yardages.html', 
                           yardages=final_display_yardages,
                           common_clubs_order=COMMON_CLUBS, # Pass this to maintain order in display
                           current_page='view_yardages')

@app.route('/add_course', methods=['GET', 'POST'])
def add_course():
    if request.method == 'POST':
        course_name = request.form.get('name', '').strip()
        pars_string = request.form.get('pars', '').strip()

        if not course_name:
            flash("Course name is required.", "danger")
            return render_template('add_course.html', current_page='add_course', default_pars=pars_string)

        pars_list = [p.strip() for p in pars_string.split(',') if p.strip().isdigit()]
        
        if len(pars_list) != 18:
            flash("Please enter exactly 18 par values, separated by commas (e.g., 4,4,3,...).", "danger")
            return render_template('add_course.html', current_page='add_course', default_pars=pars_string)

        try:
            # Check for existing course name
            existing_course = Course.query.filter_by(name=course_name).first()
            if existing_course:
                flash(f"A course named '{course_name}' already exists. Please choose a different name.", "warning")
                return render_template('add_course.html', current_page='add_course', default_pars=pars_string)

            new_course = Course(name=course_name, par_string=','.join(pars_list))
            db.session.add(new_course)
            db.session.commit()
            flash(f"Course '{course_name}' added successfully!", "success")
            return redirect(url_for('list_courses'))
        except Exception as e:
            db.session.rollback()
            app_logger.error(f"Error adding course: {e}")
            flash(f"An unexpected error occurred while adding the course. Please try again.", "danger")
            return render_template('add_course.html', current_page='add_course', default_pars=pars_string)

    return render_template('add_course.html', current_page='add_course')

@app.route('/list_courses')
def list_courses():
    courses = Course.query.order_by(Course.name).all()
    return render_template('list_courses.html', courses=courses, current_page='courses')

if __name__ == '__main__':
    with app.app_context():
        db.create_all() # Ensure tables are created
    app.run(debug=True)
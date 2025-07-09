import os
from flask import Flask, request, render_template, redirect, url_for, flash, session
from openai import OpenAI
from dotenv import load_dotenv
import base64
import io

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
    par_string = db.Column(db.String(200), nullable=False, default='3,4,5,3,4,5,3,4,5,3,4,5,3,4,5,3,4,5')
    rounds = db.relationship('Round', backref='course', lazy=True)

    def __repr__(self):
        return f"Course('{self.name}')"

    def get_pars(self):
        return [int(p) for p in self.par_string.split(',')]

    def set_pars(self, pars_list):
        self.par_string = ','.join(map(str, pars_list))

class Round(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date_played = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    total_score = db.Column(db.Integer, nullable=True) # Can be calculated or manually entered
    hole_scores = db.relationship('HoleScore', backref='round', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"Round('{self.date_played}', Course ID: {self.course_id})"

class HoleScore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey('round.id'), nullable=False)
    hole_number = db.Column(db.Integer, nullable=False) # 1 to 18
    score = db.Column(db.Integer, nullable=True)
    putts = db.Column(db.Integer, nullable=True)
    fairway_hit = db.Column(db.Boolean, nullable=True) # True/False
    gir = db.Column(db.Boolean, nullable=True) # Green In Regulation
    # Add yardage tracking fields (can be expanded)
    drive_distance = db.Column(db.Integer, nullable=True)
    approach_distance = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f"HoleScore(Round ID: {self.round_id}, Hole {self.hole_number}, Score: {self.score})"

# New Model for Club Yardages
class UserClubYardage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # For now, let's use a simple session-based user_id. In a real app, this would be a ForeignKey to a User model.
    user_identifier = db.Column(db.String(100), nullable=False)
    club_name = db.Column(db.String(50), nullable=False)
    yardage = db.Column(db.Integer, nullable=False)
    
    # Ensure unique club per user
    __table_args__ = (db.UniqueConstraint('user_identifier', 'club_name', name='_user_club_uc'),)

    def __repr__(self):
        return f"UserClubYardage(User: {self.user_identifier}, Club: {self.club_name}, Yardage: {self.yardage})"

# Create database tables if they don't exist
with app.app_context():
    db.create_all()
# --- End Database Configuration ---


# --- Define a list of common clubs ---
COMMON_CLUBS = [
    "Driver", "3 Wood", "5 Wood", "Hybrid",
    "2 Iron", "3 Iron", "4 Iron", "5 Iron", "6 Iron",
    "7 Iron", "8 Iron", "9 Iron", "Pitching Wedge",
    "Gap Wedge", "Sand Wedge", "Lob Wedge", "Putter"
]

# --- Route for the main Shot Situation Advice page ---
@app.route('/', methods=['GET'])
def index():
    """Renders the main home page."""
    # Ensure a user_identifier is set for yardage tracking
    if 'user_id' not in session:
        # This is a simple placeholder. In a real app, manage authenticated users.
        session['user_id'] = os.urandom(16).hex() # Generate a simple unique ID
        flash("Welcome! Your session ID has been set.", "info")
    
    return render_template('index.html', current_page='home') # Renamed to 'home' for consistency

@app.route('/shot_advice', methods=['GET']) # New route for shot advice
def shot_advice_page():
    """Renders the shot advice page."""
    return render_template('shot_advice.html', current_page='shot_advice')

@app.route('/ask_caddie', methods=['POST'])
def ask_caddie():
    """Handles the form submission for situational advice, sends data to AI, and displays response."""
    user_context = request.form.get('context', '') # Get text context from form
    distance_to_hole_str = request.form.get('distance_to_hole', '').strip() # NEW: Get distance to hole
    user_identifier = session.get('user_id') # Get user ID to fetch yardages

    image_base64 = None
    image_mimetype = None

    # --- Image Handling: Prioritize uploaded file over camera image ---
    if 'uploaded_image' in request.files:
        uploaded_file = request.files['uploaded_image']
        if uploaded_file.filename != '':
            try:
                image_bytes = uploaded_file.read()
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                image_mimetype = uploaded_file.mimetype
            except Exception as e:
                print(f"Error processing uploaded image for /ask_caddie: {e}")

    # If no file was uploaded, check for camera image data
    if not image_base64 and 'camera_image_data' in request.form:
        camera_data_url = request.form['camera_image_data']
        if camera_data_url:
            try:
                # Data URL format: data:<mimetype>;base64,<data>
                header, base64_data = camera_data_url.split(',', 1)
                image_mimetype = header.split(';')[0].split(':')[1]
                image_base64 = base64_data
            except Exception as e:
                print(f"Error processing camera image data for /ask_caddie: {e}")
                image_base64 = None

    # --- Fetch user's club yardages ---
    club_yardages_str = ""
    if user_identifier:
        yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_identifier).order_by(UserClubYardage.club_name).all()
        if yardage_objects:
            yardage_lines = [f"- {yc.club_name}: {yc.yardage} yards" for yc in yardage_objects]
            club_yardages_str = "\nYour Club Yardages:\n" + "\n".join(yardage_lines)
        else:
            club_yardages_str = "\nNo club yardages saved yet. Please add them in the 'Input Club Yardages' section for more tailored advice."
    else:
        club_yardages_str = "\nNo user identified. Club yardages cannot be retrieved. Please ensure your session is active."

    # --- Prepare distance to hole for AI ---
    distance_info = ""
    if distance_to_hole_str and distance_to_hole_str.isdigit():
        distance_to_hole = int(distance_to_hole_str)
        if 0 < distance_to_hole < 1000: # Sensible range for golf distances
            distance_info = f"\nDistance to target: {distance_to_hole} yards."
        else:
            flash("Invalid 'Distance to Hole' entered. Please enter a number between 1 and 999.", "warning")
    elif distance_to_hole_str: # Not empty but not a digit
        flash("Invalid 'Distance to Hole' input. Please enter a number.", "warning")


    # --- Construct the messages for OpenAI's Chat Completions API (Shot Advice) ---
    caddie_persona = (
        "Act as a professional golf caddie."
        "The golfer needs advice based on their current situation, any provided image, and their club yardages. "
        "Assess the situation, offer strategic advice (club choice, shot type, aim). "
        "When suggesting a club, *always* consider the provided 'Your Club Yardages' data and the 'Distance to target' if available. "
        "If a specific distance is mentioned, recommend a club from their list that best matches or is close to that distance. "
        "Be concise, like a good caddie."
        "Explain your advice based on the image and the context."
    )

    messages = [
        {"role": "system", "content": caddie_persona},
        {"role": "user", "content": []}
    ]
    
    # Combine user context, club yardages, and distance to hole
    full_user_input = f"Golfer's context: {user_context}{distance_info}\n\n{club_yardages_str}"
    messages[1]["content"].append({"type": "text", "text": full_user_input})

    if image_base64 and image_mimetype:
        messages[1]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{image_mimetype};base64,{image_base64}"
            }
        })
    else:
        # If no image provided at all, tell the AI that.
        messages[1]["content"].append({"type": "text", "text": "No image was provided, please provide general advice based on the text context."})

    caddie_advice = "Caddie is thinking..." # Default message

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=messages,
            max_tokens=300, # Limit response length
        )
        caddie_advice = response.choices[0].message.content

    except Exception as e:
        caddie_advice = f"Caddie's lost his focus! Error: {e}"
        print(f"OpenAI API call error for /ask_caddie: {e}")

    return render_template('shot_advice.html', 
                           caddie_advice=caddie_advice, 
                           user_context=user_context, 
                           distance_to_hole=distance_to_hole_str, # Pass back to pre-fill form
                           current_page='shot_advice')

# --- Route for Swing Analysis Page (GET request to show the form) ---
@app.route('/swing_analysis', methods=['GET']) # Changed route name from _page to be cleaner
def swing_analysis_page():
    """Renders the swing analysis page."""
    return render_template('swing_analysis.html', current_page='swing_analysis')

# --- Route to Process Swing Analysis (POST request to get feedback) ---
@app.route('/process_swing_analysis', methods=['POST'])
def process_swing_analysis():
    """Handles the swing analysis form submission, sends data to AI, and displays feedback."""
    user_context = request.form.get('context', '') # Any additional notes on the swing
    image_base64 = None
    image_mimetype = None

    # --- Image Handling: Prioritize uploaded file over camera image ---
    if 'uploaded_image' in request.files:
        uploaded_file = request.files['uploaded_image']
        if uploaded_file.filename != '':
            try:
                image_bytes = uploaded_file.read()
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                image_mimetype = uploaded_file.mimetype
            except Exception as e:
                print(f"Error processing uploaded image for /process_swing_analysis: {e}")

    # If no file was uploaded, check for camera image data
    if not image_base64 and 'camera_image_data' in request.form:
        camera_data_url = request.form['camera_image_data']
        if camera_data_url:
            try:
                header, base64_data = camera_data_url.split(',', 1)
                image_mimetype = header.split(';')[0].split(':')[1]
                image_base64 = base64_data
            except Exception as e:
                print(f"Error processing camera image data for /process_swing_analysis: {e}")
                image_base64 = None
    
    # --- Specific prompt for Swing Analysis ---
    swing_caddie_persona_and_task = (
        "You are a professional golf instructor and caddie. "
        "You are reviewing a golfer's swing captured in an image. "
        "Analyze the posture, club position (if visible), and overall swing mechanics. "
        "Provide constructive feedback, focusing on 1-2 key areas for improvement. "
        "Offer a specific, actionable tip. "
        "Remember, you're observing a *still image*, so focus on what can be inferred visually."
    )

    messages = [
        {"role": "system", "content": swing_caddie_persona_and_task},
        {"role": "user", "content": []}
    ]
    messages[1]["content"].append({"type": "text", "text": f"Golfer's notes on swing: {user_context}"})

    if image_base64 and image_mimetype:
        messages[1]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{image_mimetype};base64,{image_base64}"
            }
        })
    else:
        # If no image provided at all, tell the AI that.
        messages[1]["content"].append({"type": "text", "text": "No image was provided, please provide general advice based on the text context."})


    swing_feedback = "Caddie is studying your swing..." # Default message

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=messages,
            max_tokens=400, # Allow a bit more detail for swing feedback
        )
        swing_feedback = response.choices[0].message.content

    except Exception as e:
        swing_feedback = f"Caddie missed that swing! Error: {e}"
        print(f"OpenAI API call error for /process_swing_analysis: {e}")

    return render_template('swing_analysis.html', swing_feedback=swing_feedback, user_context=user_context, current_page='swing_analysis')


# --- New Routes for Course Management ---
@app.route('/courses', methods=['GET'])
def list_courses():
    courses = Course.query.all()
    return render_template('list_courses.html', courses=courses, current_page='courses')

@app.route('/courses/new', methods=['GET', 'POST'])
def add_course():
    if request.method == 'POST':
        name = request.form['name']
        # Pars are expected as a comma-separated string, e.g., "4,4,3,5..."
        pars_str = request.form['pars'].strip()
        
        # Basic validation for pars
        try:
            # Ensure it's a comma-separated list of numbers
            pars_list = [int(p.strip()) for p in pars_str.split(',') if p.strip()]
            if not (1 <= len(pars_list) <= 18): # Ensure 1 to 18 holes
                 flash('Please enter par values for 1 to 18 holes.', 'danger')
                 return render_template('add_course.html', current_page='add_course', default_pars=pars_str)
            if not all(1 <= p <= 6 for p in pars_list): # Typical par values
                flash('Par values should be between 1 and 6.', 'danger')
                return render_template('add_course.html', current_page='add_course', default_pars=pars_str)

        except ValueError:
            flash('Invalid par format. Please use comma-separated numbers (e.g., "4,4,3,5").', 'danger')
            return render_template('add_course.html', current_page='add_course', default_pars=pars_str)

        existing_course = Course.query.filter_by(name=name).first()
        if existing_course:
            flash(f'Course "{name}" already exists!', 'warning')
        else:
            new_course = Course(name=name, par_string=pars_str)
            db.session.add(new_course)
            db.session.commit()
            flash(f'Course "{name}" added successfully!', 'success')
            return redirect(url_for('list_courses'))
    
    # Pre-fill pars with a typical 18-hole par sequence for convenience
    default_pars = '4,4,3,5,4,4,3,5,4,4,3,5,4,4,3,5,4,4'
    return render_template('add_course.html', current_page='add_course', default_pars=default_pars)


# --- New Routes for Club Yardage Management ---
@app.route('/input_yardages', methods=['GET', 'POST'])
def input_yardages():
    user_identifier = session.get('user_id')
    if not user_identifier:
        flash("Please set a user ID (or log in) to save yardages.", "danger")
        return redirect(url_for('index')) # Redirect to home if no user_id

    if request.method == 'POST':
        # Delete existing yardages for this user to simplify updates
        UserClubYardage.query.filter_by(user_identifier=user_identifier).delete()
        db.session.commit() # Commit deletion first

        for club in COMMON_CLUBS:
            # Generate the expected form field name (e.g., 'sand_wedge')
            field_name = club.replace(" ", "_").lower()
            yardage_str = request.form.get(field_name, '').strip()

            if yardage_str and yardage_str.isdigit():
                yardage = int(yardage_str)
                if 0 < yardage < 500: # Basic sensible range for yardages
                    new_yardage_entry = UserClubYardage(
                        user_identifier=user_identifier,
                        club_name=club,
                        yardage=yardage
                    )
                    db.session.add(new_yardage_entry)
                else:
                    flash(f"Invalid yardage for {club}: '{yardage_str}'. Must be a number between 1 and 499.", 'warning')
            elif yardage_str: # Not empty but not a digit
                 flash(f"Invalid input for {club}: '{yardage_str}'. Please enter a number.", 'warning')
        
        try:
            db.session.commit()
            flash('Your club yardages have been saved!', 'success')
            return redirect(url_for('view_yardages'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error saving yardages: {e}", 'danger')
            print(f"Error saving yardages: {e}")


    # GET request: Display the form
    # Load existing yardages for the current user
    # Convert list of objects to a dictionary for easy template access
    existing_yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_identifier).all()
    current_yardages_dict = {yc.club_name: yc.yardage for yc in existing_yardage_objects}

    return render_template('input_yardages.html', 
                           clubs=COMMON_CLUBS, 
                           current_yardages=current_yardages_dict,
                           current_page='input_yardages')

@app.route('/view_yardages')
def view_yardages():
    user_identifier = session.get('user_id')
    if not user_identifier:
        flash("Please set a user ID (or log in) to view yardages.", "danger")
        return redirect(url_for('index'))

    yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_identifier).order_by(UserClubYardage.club_name).all()
    
    # Sort clubs in a custom order if desired, otherwise alphabetical by club_name
    # For now, let's keep it simple and just use the retrieved order or alphabetical
    
    # You could re-sort based on COMMON_CLUBS order for a consistent display
    sorted_yardages = {club: None for club in COMMON_CLUBS}
    for obj in yardage_objects:
        sorted_yardages[obj.club_name] = obj.yardage
    
    # Filter out clubs that don't have a yardage (or were not in COMMON_CLUBS for some reason)
    display_yardages = {club: yardage for club, yardage in sorted_yardages.items() if yardage is not None}

    return render_template('view_yardages.html', 
                           yardages=display_yardages,
                           common_clubs_order=COMMON_CLUBS, # Pass this to maintain order in display
                           current_page='view_yardages')

if __name__ == '__main__':
    # It's good practice to create tables within the app context before running
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=8000)
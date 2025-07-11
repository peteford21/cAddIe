import os
from flask import Flask, request, render_template, redirect, url_for, flash, session
from openai import OpenAI
from dotenv import load_dotenv
import base64
import logging
from markdown2 import markdown
from datetime import datetime

# --- Database Imports ---
from flask_sqlalchemy import SQLAlchemy

# Load environment variables from .env file
load_dotenv()

# --- App Initialization and Configuration ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a_very_secret_key_for_your_golf_caddie_app_CHANGE_THIS")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///golf_caddie.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

# --- Services Initialization ---
db = SQLAlchemy(app)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
AI_MODEL_NAME = "gpt-4o"

# --- Database Models ---
class Course(db.Model):
    """Represents a golf course."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    par_string = db.Column(db.String(200), nullable=False, default=','.join(['4']*18)) # Default to all par 4s

    def get_pars(self):
        return [int(p) for p in self.par_string.split(',')]

    def __repr__(self):
        return f'<Course {self.name}>'

class Round(db.Model):
    """Represents a single round of golf played by a user."""
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    user_identifier = db.Column(db.String(255), nullable=False, index=True)
    date_played = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    scores_string = db.Column(db.String(200), nullable=True) # Stored as comma-separated values
    is_complete = db.Column(db.Boolean, default=False)
    course = db.relationship('Course', backref=db.backref('rounds', lazy=True, cascade="all, delete-orphan"))

    def get_scores(self):
        return [int(s) if s else 0 for s in self.scores_string.split(',')] if self.scores_string else []

    def calculate_total_score(self):
        return sum(self.get_scores())

    def calculate_score_to_par(self):
        course_pars = self.course.get_pars()
        round_scores = self.get_scores()
        # Only calculate for holes played
        played_holes = len(round_scores)
        if played_holes == 0:
            return 0
        return sum(round_scores[i] - course_pars[i] for i in range(played_holes))

class UserClubYardage(db.Model):
    """Stores the yardage for each club for a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_identifier = db.Column(db.String(255), nullable=False, index=True)
    club_name = db.Column(db.String(50), nullable=False)
    yardage = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_identifier', 'club_name', name='_user_club_uc'),)

    def __repr__(self):
        return f'<UserClubYardage {self.user_identifier} - {self.club_name}: {self.yardage}>'

# --- Constants ---
COMMON_CLUBS = [
    "Driver", "3-Wood", "5-Wood", "Hybrid", "2-Iron", "3-Iron", "4-Iron",
    "5-Iron", "6-Iron", "7-Iron", "8-Iron", "9-Iron", "Pitching Wedge",
    "Gap Wedge", "Sand Wedge", "Lob Wedge"
]

# --- Helper Functions ---
@app.context_processor
def inject_now():
    """Injects the current datetime into all templates."""
    return {'now': datetime.utcnow()}

def get_user_id():
    """Ensures a user has a persistent session ID."""
    if 'user_id' not in session:
        session['user_id'] = os.urandom(24).hex()
    return session['user_id']

def encode_image(image_input):
    """Encodes an image from file storage or data URL to a base64 string."""
    if isinstance(image_input, str) and image_input.startswith('data:image'):
        return image_input.split(',')[1]
    if hasattr(image_input, 'read'):
        image_input.seek(0)
        return base64.b64encode(image_input.read()).decode('utf-8')
    return None

def get_ai_response(prompt_text, image_base64=None, system_message="You are a helpful AI assistant."):
    """Gets a response from the OpenAI API."""
    messages = [{"role": "system", "content": system_message}]
    user_content = [{"type": "text", "text": prompt_text}]
    if image_base64:
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
    messages.append({"role": "user", "content": user_content})

    try:
        response = client.chat.completions.create(model=AI_MODEL_NAME, messages=messages, max_tokens=700)
        # Convert markdown response to HTML
        return markdown(response.choices[0].message.content)
    except Exception as e:
        app_logger.error(f"OpenAI API error: {e}")
        return f"<p class='error-message'>An error occurred while getting AI response: {e}</p>"

# --- Routes ---
@app.route('/')
def index():
    """Renders the main home page."""
    user_id = get_user_id()
    return render_template('index.html', current_page='home')

@app.route('/shot_advice', methods=['GET', 'POST'])
def shot_advice():
    """Handles the shot advice feature, including form submission."""
    user_id = get_user_id()
    if request.method == 'POST':
        user_situation = request.form.get('situation', '').strip()
        target_yardage = request.form.get('yardage', '').strip()
        image_file = request.files.get('image_file')
        camera_image_data = request.form.get('camera_image_data')

        image_base64 = encode_image(image_file or camera_image_data)

        if not user_situation and not image_base64 and not target_yardage:
            flash("Please provide a situation, yardage, or an image.", "danger")
            return redirect(url_for('shot_advice'))

        # Build a detailed prompt for the AI
        prompt_parts = []
        user_yardages = UserClubYardage.query.filter_by(user_identifier=user_id).all()
        if user_yardages:
            yardage_info = ", ".join([f"{y.club_name}: {y.yardage} yards" for y in user_yardages])
            prompt_parts.append(f"My typical club yardages are: {yardage_info}.")
        else:
            prompt_parts.append("I have no saved club yardages. Base club suggestions on typical amateur distances.")

        if user_situation:
            prompt_parts.append(f"The situation is: {user_situation}.")
        if target_yardage:
            prompt_parts.append(f"The distance to the target is {target_yardage} yards.")

        prompt = " ".join(prompt_parts)
        system_message = (
            "You are an expert golf caddie. Analyze the situation, considering the user's club distances if provided. "
            "Offer clear, strategic advice on club selection, shot type, and aiming point. "
            "Format your response using markdown with headings for 'Overall Strategy', 'Club Suggestion', and 'Execution Tips'."
        )
        ai_advice = get_ai_response(prompt, image_base64, system_message)
        return ai_advice # Return raw HTML for AJAX call

    return render_template('shot_advice.html', current_page='shot_advice')

@app.route('/swing_analysis', methods=['GET', 'POST'])
def swing_analysis():
    """Handles the swing analysis feature."""
    user_id = get_user_id()
    if request.method == 'POST':
        user_notes = request.form.get('notes', '').strip()
        image_file = request.files.get('image_file')
        camera_image_data = request.form.get('camera_image_data')

        image_base64 = encode_image(image_file or camera_image_data)

        if not image_base64:
            return "<p class='error-message'>An image is required for swing analysis.</p>", 400

        prompt = "Analyze the golf swing in the image. "
        if user_notes:
            prompt += f"The user noted: '{user_notes}'. "
        prompt += "Provide constructive feedback on posture, grip, and key swing positions. Suggest specific drills for improvement."

        system_message = (
            "You are an expert golf swing coach. Analyze the provided image of a golf swing. "
            "Provide clear, actionable feedback. Use markdown to structure your analysis with headings for "
            "'Key Observations', 'Areas for Improvement', and 'Recommended Drills'."
        )
        ai_analysis = get_ai_response(prompt, image_base64, system_message)
        return ai_analysis # Return raw HTML for AJAX call

    return render_template('swing_analysis.html', current_page='swing_analysis')

@app.route('/yardages', methods=['GET', 'POST'])
def yardages():
    """Handles input and viewing of club yardages."""
    user_id = get_user_id()
    if request.method == 'POST':
        for club in COMMON_CLUBS:
            club_input_name = club.replace('-', '_').lower().replace(' ', '_')
            yardage_str = request.form.get(club_input_name)
            existing_yardage = UserClubYardage.query.filter_by(user_identifier=user_id, club_name=club).first()

            if yardage_str and yardage_str.isdigit():
                yardage = int(yardage_str)
                if existing_yardage:
                    existing_yardage.yardage = yardage
                else:
                    new_yardage = UserClubYardage(user_identifier=user_id, club_name=club, yardage=yardage)
                    db.session.add(new_yardage)
            elif existing_yardage:
                db.session.delete(existing_yardage)
        try:
            db.session.commit()
            flash("Your club yardages have been saved!", "success")
        except Exception as e:
            db.session.rollback()
            app_logger.error(f"Error saving yardages: {e}")
            flash("An error occurred while saving yardages.", "danger")
        return redirect(url_for('yardages'))

    current_yardages = {y.club_name: y.yardage for y in UserClubYardage.query.filter_by(user_identifier=user_id).all()}
    return render_template('yardages.html', clubs=COMMON_CLUBS, current_yardages=current_yardages, current_page='yardages')

@app.route('/courses', methods=['GET', 'POST'])
def list_courses():
    """Lists all courses and handles adding new ones."""
    if request.method == 'POST':
        course_name = request.form.get('name', '').strip()
        pars_list = [p.strip() for p in request.form.get('pars', '').split(',') if p.strip().isdigit()]

        if not course_name or len(pars_list) != 18:
            flash("Course name and exactly 18 par values are required.", "danger")
        elif Course.query.filter_by(name=course_name).first():
            flash(f"A course named '{course_name}' already exists.", "warning")
        else:
            new_course = Course(name=course_name, par_string=','.join(pars_list))
            db.session.add(new_course)
            db.session.commit()
            flash(f"Course '{course_name}' added successfully!", "success")
        return redirect(url_for('list_courses'))

    courses = Course.query.order_by(Course.name).all()
    return render_template('list_courses.html', courses=courses, current_page='courses')

@app.route('/courses/delete/<int:course_id>', methods=['POST'])
def delete_course(course_id):
    """Deletes a course."""
    course = Course.query.get_or_404(course_id)
    try:
        db.session.delete(course)
        db.session.commit()
        flash(f"Course '{course.name}' has been deleted.", "success")
    except Exception as e:
        db.session.rollback()
        app_logger.error(f"Error deleting course: {e}")
        flash("Error deleting course. It may be associated with saved rounds.", "danger")
    return redirect(url_for('list_courses'))


@app.route('/track_round', methods=['GET', 'POST'])
def track_round_start():
    """Page to start tracking a new round."""
    user_id = get_user_id()
    if request.method == 'POST':
        course_id = request.form.get('course_id')
        if not course_id:
            flash("Please select a course to start a round.", "warning")
            return redirect(url_for('track_round_start'))

        # Check for an incomplete round for this user
        incomplete_round = Round.query.filter_by(user_identifier=user_id, is_complete=False).first()
        if incomplete_round:
            flash("You have an unfinished round. Please complete or delete it before starting a new one.", "warning")
            return redirect(url_for('track_round_live', round_id=incomplete_round.id))

        new_round = Round(course_id=course_id, user_identifier=user_id, scores_string="")
        db.session.add(new_round)
        db.session.commit()
        flash("New round started! Let's play.", "success")
        return redirect(url_for('track_round_live', round_id=new_round.id))

    courses = Course.query.order_by(Course.name).all()
    return render_template('track_round_start.html', courses=courses, current_page='track_round')

@app.route('/track_round/<int:round_id>', methods=['GET', 'POST'])
def track_round_live(round_id):
    """Live scorecard for an active round."""
    user_id = get_user_id()
    live_round = Round.query.get_or_404(round_id)

    if live_round.user_identifier != user_id:
        flash("You are not authorized to view this round.", "danger")
        return redirect(url_for('index'))

    if live_round.is_complete:
        flash("This round has already been completed.", "info")
        return redirect(url_for('list_rounds'))

    if request.method == 'POST':
        scores = []
        for i in range(1, 19):
            score = request.form.get(f'hole_{i}')
            scores.append(score if score else '') # Keep empty strings for not-yet-played holes

        live_round.scores_string = ','.join(scores)

        # Check if the round should be marked as complete
        if all(s.isdigit() and int(s) > 0 for s in scores):
             live_round.is_complete = True
             flash("Round complete! Well played.", "success")
        else:
            flash("Scores saved successfully.", "success")

        db.session.commit()
        if live_round.is_complete:
            return redirect(url_for('list_rounds'))
        return redirect(url_for('track_round_live', round_id=round_id))

    course_pars = live_round.course.get_pars()
    round_scores = [s if s else '' for s in live_round.scores_string.split(',')] if live_round.scores_string else [''] * 18
    # Ensure scores list is 18 items long for the template
    while len(round_scores) < 18:
        round_scores.append('')

    return render_template('track_round_live.html',
                           live_round=live_round,
                           course_pars=course_pars,
                           round_scores=round_scores,
                           current_page='track_round')

@app.route('/rounds')
def list_rounds():
    """Displays a list of all completed rounds for the user."""
    user_id = get_user_id()
    # Also find any incomplete round to offer resuming it
    incomplete_round = Round.query.filter_by(user_identifier=user_id, is_complete=False).first()
    completed_rounds = Round.query.filter_by(user_identifier=user_id, is_complete=True).order_by(Round.date_played.desc()).all()
    return render_template('list_rounds.html',
                           completed_rounds=completed_rounds,
                           incomplete_round=incomplete_round,
                           current_page='rounds')

@app.route('/rounds/delete/<int:round_id>', methods=['POST'])
def delete_round(round_id):
    """Deletes a specific round."""
    user_id = get_user_id()
    round_to_delete = Round.query.get_or_404(round_id)
    if round_to_delete.user_identifier != user_id:
        flash("You are not authorized to delete this round.", "danger")
        return redirect(url_for('list_rounds'))

    db.session.delete(round_to_delete)
    db.session.commit()
    flash("Round has been successfully deleted.", "success")
    return redirect(url_for('list_rounds'))


# --- Application Context and Execution ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all() # Ensure all database tables are created before running
    app.run(debug=True, port=5001)

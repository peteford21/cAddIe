import os
from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify, Response
from openai import OpenAI
from dotenv import load_dotenv
import base64
import logging
from markdown2 import markdown
from datetime import datetime
from io import BytesIO

# --- Image Generation ---
from PIL import Image, ImageDraw, ImageFont

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
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    par_string = db.Column(db.String(200), nullable=False, default=','.join(['4']*18))
    rating = db.Column(db.Float, nullable=False, default=72.0)
    slope = db.Column(db.Integer, nullable=False, default=113)

    def get_pars(self):
        return [int(p) for p in self.par_string.split(',')]

class Round(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    user_identifier = db.Column(db.String(255), nullable=False, index=True)
    date_played = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    scores_string = db.Column(db.String(200), nullable=True)
    is_complete = db.Column(db.Boolean, default=False)
    course = db.relationship('Course', backref=db.backref('rounds', lazy=True, cascade="all, delete-orphan"))

    def get_scores(self):
        return [int(s) if s and s.isdigit() else 0 for s in self.scores_string.split(',')] if self.scores_string else []

    def calculate_total_score(self):
        return sum(s for s in self.get_scores() if s > 0)

    def calculate_score_to_par(self):
        course_pars = self.course.get_pars()
        round_scores = self.get_scores()
        played_holes = len([s for s in round_scores if s > 0])
        if played_holes == 0: return 0
        total_par_for_played_holes = sum(course_pars[i] for i, score in enumerate(round_scores) if score > 0)
        return self.calculate_total_score() - total_par_for_played_holes

class UserClubYardage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_identifier = db.Column(db.String(255), nullable=False, index=True)
    club_name = db.Column(db.String(50), nullable=False)
    yardage = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_identifier', 'club_name', name='_user_club_uc'),)

class Achievement(db.Model):
    """Defines all possible achievements in the app."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    icon_class = db.Column(db.String(50), nullable=False) # e.g., 'fas fa-trophy'

class UserAchievement(db.Model):
    """Links users to the achievements they have earned."""
    id = db.Column(db.Integer, primary_key=True)
    user_identifier = db.Column(db.String(255), nullable=False, index=True)
    achievement_id = db.Column(db.Integer, db.ForeignKey('achievement.id'), nullable=False)
    date_awarded = db.Column(db.DateTime, default=datetime.utcnow)
    achievement = db.relationship('Achievement')


# --- Constants ---
COMMON_CLUBS = [
    "Driver", "3-Wood", "5-Wood", "Hybrid", "2-Iron", "3-Iron", "4-Iron",
    "5-Iron", "6-Iron", "7-Iron", "8-Iron", "9-Iron", "Pitching Wedge",
    "Gap Wedge", "Sand Wedge", "Lob Wedge"
]

# --- Helper Functions ---
@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

def get_user_id():
    if 'user_id' not in session:
        session['user_id'] = os.urandom(24).hex()
    return session['user_id']

def get_ai_response(prompt, image_base64=None, system_message=None):
    """Get AI response from OpenAI with proper error handling."""
    try:
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        
        if image_base64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]
            })
        else:
            messages.append({"role": "user", "content": prompt})
        
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=messages,
            max_tokens=1000
        )
        
        ai_response = response.choices[0].message.content
        return markdown(ai_response)
        
    except Exception as e:
        app_logger.error(f"AI API error: {e}")
        return "<p class='error-message'>Sorry, I'm having trouble analyzing this right now. Please try again later.</p>"

def check_and_award_achievements(user_id, completed_round):
    """Checks for and awards new achievements after a round is completed."""
    all_achievements = Achievement.query.all()
    user_achievements = UserAchievement.query.filter_by(user_identifier=user_id).all()
    user_achievement_ids = {ua.achievement_id for ua in user_achievements}
    
    # --- Achievement Logic ---
    all_rounds = Round.query.filter_by(user_identifier=user_id, is_complete=True).all()
    
    for ach in all_achievements:
        if ach.id in user_achievement_ids:
            continue # Already earned

        awarded = False
        # Round count achievements
        if ach.name == "First Round" and len(all_rounds) >= 1: awarded = True
        if ach.name == "Five Rounds" and len(all_rounds) >= 5: awarded = True
        
        # Scoring achievements (only for the round just completed)
        total_score = completed_round.calculate_total_score()
        if ach.name == "Broke 100" and total_score < 100: awarded = True
        if ach.name == "Broke 90" and total_score < 90: awarded = True
        if ach.name == "Broke 80" and total_score < 80: awarded = True
        
        # Hole score achievements
        scores = completed_round.get_scores()
        pars = completed_round.course.get_pars()
        for i, score in enumerate(scores):
            if score > 0 and pars[i] > 0:
                if ach.name == "First Birdie" and score <= pars[i] - 1: awarded = True
                if ach.name == "First Eagle" and score <= pars[i] - 2: awarded = True

        if awarded:
            new_award = UserAchievement(user_identifier=user_id, achievement_id=ach.id)
            db.session.add(new_award)
            flash(f"Achievement Unlocked: {ach.name}!", "success")
    
    db.session.commit()

def calculate_handicap(user_id):
    eligible_rounds = Round.query.filter_by(user_identifier=user_id, is_complete=True)\
        .join(Course).order_by(Round.date_played.desc()).limit(20).all()
    differentials = []
    for r in eligible_rounds:
        if len(r.get_scores()) == 18 and all(s > 0 for s in r.get_scores()):
            total_score = r.calculate_total_score()
            differential = (total_score - r.course.rating) * 113 / r.course.slope
            differentials.append(differential)
    if not differentials: return None
    differentials.sort()
    num_to_average = min(8, len(differentials)) if len(differentials) >= 8 else (len(differentials) // 2 if len(differentials) >= 5 else (2 if len(differentials) >= 3 else 1))
    best_differentials = differentials[:num_to_average]
    return round(sum(best_differentials) / len(best_differentials), 1)

# --- Routes ---
@app.route('/')
def index():
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

        image_base64 = base64.b64encode(image_file.read()).decode('utf-8') if image_file else (camera_image_data.split(',')[1] if camera_image_data else None)

        if not user_situation and not image_base64 and not target_yardage:
            flash("Please provide a situation, yardage, or an image.", "danger")
            return redirect(url_for('shot_advice'))

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
        return ai_advice

    return render_template('shot_advice.html', current_page='shot_advice')

@app.route('/swing_analysis', methods=['GET', 'POST'])
def swing_analysis():
    """Handles the swing analysis feature."""
    user_id = get_user_id()
    if request.method == 'POST':
        user_notes = request.form.get('notes', '').strip()
        image_file = request.files.get('image_file')
        camera_image_data = request.form.get('camera_image_data')

        image_base64 = base64.b64encode(image_file.read()).decode('utf-8') if image_file else (camera_image_data.split(',')[1] if camera_image_data else None)

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
        return ai_analysis

    return render_template('swing_analysis.html', current_page='swing_analysis')

@app.route('/yardages', methods=['GET', 'POST'])
def yardages():
    """Handles input and viewing of club yardages, including data for the gapping chart."""
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

    # Prepare data for the gapping chart
    yardage_objects = UserClubYardage.query.filter_by(user_identifier=user_id).all()
    user_yardages_dict = {y.club_name: y.yardage for y in yardage_objects}
    
    chart_data = {
        "labels": [club for club in COMMON_CLUBS if club in user_yardages_dict],
        "values": [user_yardages_dict[club] for club in COMMON_CLUBS if club in user_yardages_dict]
    }
    if chart_data["labels"]:
        sorted_pairs = sorted(zip(chart_data["values"], chart_data["labels"]), reverse=True)
        chart_data["values"], chart_data["labels"] = zip(*sorted_pairs)

    return render_template('yardages.html', 
                           clubs=COMMON_CLUBS, 
                           current_yardages=user_yardages_dict, 
                           chart_data=chart_data,
                           current_page='yardages')

@app.route('/courses', methods=['GET', 'POST'])
def list_courses():
    """Lists all courses and handles adding new ones with rating and slope."""
    if request.method == 'POST':
        course_name = request.form.get('name', '').strip()
        pars_list = [p.strip() for p in request.form.get('pars', '').split(',') if p.strip().isdigit()]
        rating = request.form.get('rating')
        slope = request.form.get('slope')

        if not all([course_name, rating, slope]) or len(pars_list) != 18:
            flash("All fields (Name, Pars, Rating, Slope) are required, and pars must have 18 values.", "danger")
        elif Course.query.filter_by(name=course_name).first():
            flash(f"A course named '{course_name}' already exists.", "warning")
        else:
            try:
                new_course = Course(name=course_name, 
                                    par_string=','.join(pars_list),
                                    rating=float(rating),
                                    slope=int(slope))
                db.session.add(new_course)
                db.session.commit()
                flash(f"Course '{course_name}' added successfully!", "success")
            except (ValueError, TypeError):
                 flash("Invalid input for Rating or Slope. Please enter numbers.", "danger")
            except Exception as e:
                 db.session.rollback()
                 app_logger.error(f"Error adding course: {e}")
                 flash("An unexpected error occurred.", "danger")
        return redirect(url_for('list_courses'))

    courses = Course.query.order_by(Course.name).all()
    return render_template('list_courses.html', courses=courses, current_page='courses')

@app.route('/track_round_start', methods=['GET', 'POST'])
def track_round_start():
    """Handles starting a new round - shows course selection page or creates new round."""
    user_id = get_user_id()
    
    if request.method == 'POST':
        course_id = request.form.get('course_id')
        if not course_id:
            flash("Please select a course.", "danger")
            return redirect(url_for('track_round_start'))
        
        # Check if user already has an incomplete round
        existing_round = Round.query.filter_by(user_identifier=user_id, is_complete=False).first()
        if existing_round:
            flash("You already have an active round. Please complete or delete it first.", "warning")
            return redirect(url_for('list_rounds'))
        
        # Create new round
        new_round = Round(course_id=course_id, user_identifier=user_id)
        db.session.add(new_round)
        db.session.commit()
        
        flash("New round started! Enter your scores as you play.", "success")
        return redirect(url_for('track_round_live', round_id=new_round.id))
    
    courses = Course.query.order_by(Course.name).all()
    return render_template('track_round_start.html', courses=courses, current_page='track_round')

@app.route('/track_round/<int:round_id>', methods=['GET', 'POST'])
def track_round_live(round_id):
    user_id = get_user_id()
    live_round = Round.query.get_or_404(round_id)
    if live_round.user_identifier != user_id:
        flash("You are not authorized to view this round.", "danger")
        return redirect(url_for('index'))

    if request.method == 'POST':
        scores = [request.form.get(f'hole_{i}', '') for i in range(1, 19)]
        live_round.scores_string = ','.join(scores)
        if all(s.isdigit() and int(s) > 0 for s in scores):
             live_round.is_complete = True
             db.session.commit() # Commit before checking achievements
             check_and_award_achievements(user_id, live_round)
             flash("Round complete! Well played.", "success")
        else:
            flash("Scores saved successfully.", "success")
            db.session.commit()
        return redirect(url_for('track_round_live', round_id=round_id))

    course_pars = live_round.course.get_pars()
    round_scores = live_round.scores_string.split(',') if live_round.scores_string else [''] * 18
    return render_template('track_round_live.html', live_round=live_round, course_pars=course_pars, round_scores=round_scores, current_page='track_round')

@app.route('/rounds')
def list_rounds():
    user_id = get_user_id()
    handicap = calculate_handicap(user_id)
    incomplete_round = Round.query.filter_by(user_identifier=user_id, is_complete=False).first()
    completed_rounds = Round.query.filter_by(user_identifier=user_id, is_complete=True).order_by(Round.date_played.desc()).all()
    return render_template('list_rounds.html', completed_rounds=completed_rounds, incomplete_round=incomplete_round, handicap=handicap, current_page='rounds')

@app.route('/delete_round/<int:round_id>', methods=['POST'])
def delete_round(round_id):
    """Handles deleting a round (both complete and incomplete)."""
    user_id = get_user_id()
    round_to_delete = Round.query.get_or_404(round_id)
    
    if round_to_delete.user_identifier != user_id:
        flash("You are not authorized to delete this round.", "danger")
        return redirect(url_for('index'))
    
    try:
        db.session.delete(round_to_delete)
        db.session.commit()
        flash("Round deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        app_logger.error(f"Error deleting round: {e}")
        flash("An error occurred while deleting the round.", "danger")
    
    return redirect(url_for('list_rounds'))

@app.route('/achievements')
def achievements():
    """Displays the user's earned achievements."""
    user_id = get_user_id()
    user_achievements = UserAchievement.query.filter_by(user_identifier=user_id).join(Achievement).order_by(UserAchievement.date_awarded.desc()).all()
    return render_template('achievements.html', user_achievements=user_achievements, current_page='achievements')

@app.route('/share_scorecard/<int:round_id>')
def share_scorecard(round_id):
    """Generates a shareable image of a completed scorecard."""
    s_round = Round.query.get_or_404(round_id)
    
    # --- Image Creation ---
    width, height = 800, 1000
    bg_color = (240, 248, 240)
    img = Image.new('RGB', (width, height), color=bg_color)
    d = ImageDraw.Draw(img)
    
    # --- Fonts (assumes a default font is available, otherwise specify path) ---
    try:
        font_h1 = ImageFont.truetype("arialbd.ttf", 40)
        font_h2 = ImageFont.truetype("arialbd.ttf", 24)
        font_p = ImageFont.truetype("arial.ttf", 18)
        font_table = ImageFont.truetype("arial.ttf", 16)
    except IOError:
        font_h1 = ImageFont.load_default()
        font_h2 = ImageFont.load_default()
        font_p = ImageFont.load_default()
        font_table = ImageFont.load_default()

    # --- Drawing ---
    d.text((width/2, 50), "AI Golf Caddie", font=font_h1, fill=(0,100,0), anchor="mm")
    d.text((width/2, 100), s_round.course.name, font=font_h2, fill=(30,30,30), anchor="mm")
    d.text((width/2, 130), s_round.date_played.strftime('%B %d, %Y'), font=font_p, fill=(80,80,80), anchor="mm")

    # Table headers
    cols = ["Hole", "Par", "Score"]
    col_w = 150
    start_x = (width - col_w * len(cols)) / 2
    y = 200
    for i, header in enumerate(cols):
        d.text((start_x + i * col_w + col_w/2, y), header, font=font_h2, fill=(0,0,0), anchor="mm")
    
    # Table rows
    pars = s_round.course.get_pars()
    scores = s_round.get_scores()
    out_par, in_par, out_score, in_score = 0, 0, 0, 0
    
    for i in range(18):
        row_y = y + 40 + (i * 30)
        if i == 9: # Separator and Out scores
            d.line([(start_x, row_y-15), (start_x + col_w * 3, row_y-15)], fill=(150,150,150), width=2)
            d.text((start_x + col_w/2, row_y), "OUT", font=font_h2, fill=(0,0,0), anchor="mm")
            d.text((start_x + col_w + col_w/2, row_y), str(out_par), font=font_h2, fill=(0,0,0), anchor="mm")
            d.text((start_x + 2*col_w + col_w/2, row_y), str(out_score), font=font_h2, fill=(0,0,0), anchor="mm")
            row_y += 40 # Add extra space
        
        d.text((start_x + col_w/2, row_y), str(i+1), font=font_table, fill=(50,50,50), anchor="mm")
        d.text((start_x + col_w + col_w/2, row_y), str(pars[i]), font=font_table, fill=(50,50,50), anchor="mm")
        d.text((start_x + 2*col_w + col_w/2, row_y), str(scores[i]), font=font_table, fill=(0,100,0), anchor="mm")
        
        if i < 9:
            out_par += pars[i]
            out_score += scores[i]
        else:
            in_par += pars[i]
            in_score += scores[i]

    # In scores
    row_y += 40
    d.line([(start_x, row_y-15), (start_x + col_w * 3, row_y-15)], fill=(150,150,150), width=2)
    d.text((start_x + col_w/2, row_y), "IN", font=font_h2, fill=(0,0,0), anchor="mm")
    d.text((start_x + col_w + col_w/2, row_y), str(in_par), font=font_h2, fill=(0,0,0), anchor="mm")
    d.text((start_x + 2*col_w + col_w/2, row_y), str(in_score), font=font_h2, fill=(0,0,0), anchor="mm")
    
    # Total scores
    row_y += 40
    d.line([(start_x, row_y-15), (start_x + col_w * 3, row_y-15)], fill=(150,150,150), width=2)
    d.text((start_x + col_w/2, row_y), "TOTAL", font=font_h2, fill=(0,0,0), anchor="mm")
    d.text((start_x + col_w + col_w/2, row_y), str(out_par + in_par), font=font_h2, fill=(0,0,0), anchor="mm")
    d.text((start_x + 2*col_w + col_w/2, row_y), str(out_score + in_score), font=font_h2, fill=(0,100,0), anchor="mm")

    # --- Serve Image ---
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return Response(img_io.getvalue(), mimetype='image/png')

# --- Application Context and Execution ---
def setup_database(app):
    with app.app_context():
        db.create_all()
        # Seed achievements if they don't exist
        if Achievement.query.count() == 0:
            achievements_to_add = [
                Achievement(name="First Round", description="Complete your first 18-hole round.", icon_class="fas fa-flag-checkered"),
                Achievement(name="Five Rounds", description="Complete five 18-hole rounds.", icon_class="fas fa-layer-group"),
                Achievement(name="First Birdie", description="Score a birdie or better on any hole.", icon_class="fas fa-dove"),
                Achievement(name="First Eagle", description="Score an eagle or better on any hole.", icon_class="fas fa-feather-alt"),
                Achievement(name="Broke 100", description="Finish a round with a score under 100.", icon_class="fas fa-glass-cheers"),
                Achievement(name="Broke 90", description="Finish a round with a score under 90.", icon_class="fas fa-medal"),
                Achievement(name="Broke 80", description="Finish a round with a score under 80.", icon_class="fas fa-trophy"),
            ]
            db.session.bulk_save_objects(achievements_to_add)
            db.session.commit()

if __name__ == '__main__':
    setup_database(app)
    app.run(debug=True, port=5001)
import os
import json
import sqlite3
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, 
                   session, flash, jsonify, g)
from werkzeug.security import generate_password_hash, check_password_hash
import requests

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['DATABASE'] = os.path.join(os.path.dirname(__file__), 'skillswap.db')

# Gemini AI Setup
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')

if GEMINI_AVAILABLE and GEMINI_API_KEY and GEMINI_API_KEY != 'your_gemini_api_key_here':
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.0-flash')
else:
    gemini_model = None

# ─── Database ───────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(app.config['DATABASE'])
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            college TEXT DEFAULT '',
            city TEXT DEFAULT '',
            bio TEXT DEFAULT '',
            github_username TEXT DEFAULT '',
            avatar_url TEXT DEFAULT '',
            overall_score INTEGER DEFAULT 0,
            want_to_learn TEXT DEFAULT '[]',
            can_teach TEXT DEFAULT '[]',
            availability TEXT DEFAULT 'Flexible',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            skill_name TEXT NOT NULL,
            skill_value INTEGER DEFAULT 0,
            category TEXT DEFAULT 'General',
            verified_via TEXT DEFAULT 'self',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            match_score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user1_id) REFERENCES users(id),
            FOREIGN KEY (user2_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            scheduled_at TEXT,
            duration_mins INTEGER DEFAULT 30,
            status TEXT DEFAULT 'requested',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches(id),
            FOREIGN KEY (requester_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (sender_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            rating INTEGER DEFAULT 5,
            comment TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (reviewer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS github_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            github_username TEXT NOT NULL,
            analysis_data TEXT NOT NULL,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            badge_name TEXT NOT NULL,
            badge_icon TEXT DEFAULT '🏆',
            description TEXT DEFAULT '',
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    ''')
    db.commit()
    db.close()

# ─── Auth Decorators ────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        db = get_db()
        return db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    return None

# ─── GitHub API Integration ────────────────────────────────────────────

def fetch_github_data(username):
    """Fetch comprehensive GitHub data for analysis."""
    headers = {}
    if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here_optional':
        headers['Authorization'] = f'token {GITHUB_TOKEN}'
    
    try:
        # User profile
        user_resp = requests.get(f'https://api.github.com/users/{username}', headers=headers, timeout=10)
        if user_resp.status_code != 200:
            return None
        user_data = user_resp.json()
        
        # Repos
        repos_resp = requests.get(
            f'https://api.github.com/users/{username}/repos?sort=updated&per_page=30',
            headers=headers, timeout=10
        )
        repos_data = repos_resp.json() if repos_resp.status_code == 200 else []
        
        # Aggregate languages
        languages = {}
        repo_details = []
        for repo in repos_data[:15]:
            if repo.get('fork'):
                continue
            lang_resp = requests.get(repo.get('languages_url', ''), headers=headers, timeout=10)
            if lang_resp.status_code == 200:
                for lang, bytes_count in lang_resp.json().items():
                    languages[lang] = languages.get(lang, 0) + bytes_count
            
            repo_details.append({
                'name': repo.get('name', ''),
                'description': repo.get('description', '') or 'No description',
                'language': repo.get('language', 'Unknown'),
                'stars': repo.get('stargazers_count', 0),
                'forks': repo.get('forks_count', 0),
                'topics': repo.get('topics', []),
                'updated_at': repo.get('updated_at', ''),
                'has_readme': True,
                'size': repo.get('size', 0),
            })
        
        # Events for activity analysis
        events_resp = requests.get(
            f'https://api.github.com/users/{username}/events?per_page=100',
            headers=headers, timeout=10
        )
        events_data = events_resp.json() if events_resp.status_code == 200 else []
        
        # Count recent activity
        push_events = len([e for e in events_data if e.get('type') == 'PushEvent'])
        pr_events = len([e for e in events_data if e.get('type') == 'PullRequestEvent'])
        issue_events = len([e for e in events_data if e.get('type') == 'IssuesEvent'])
        
        return {
            'profile': {
                'name': user_data.get('name', username),
                'bio': user_data.get('bio', ''),
                'avatar_url': user_data.get('avatar_url', ''),
                'public_repos': user_data.get('public_repos', 0),
                'followers': user_data.get('followers', 0),
                'following': user_data.get('following', 0),
                'created_at': user_data.get('created_at', ''),
            },
            'languages': languages,
            'repos': repo_details,
            'activity': {
                'push_events': push_events,
                'pr_events': pr_events,
                'issue_events': issue_events,
                'total_events': len(events_data),
            }
        }
    except Exception as e:
        print(f"GitHub API error: {e}")
        return None

def analyze_with_ai(github_data):
    """Use Gemini AI to analyze GitHub profile and assign skill values."""
    if not gemini_model:
        return generate_fallback_analysis(github_data)
    
    prompt = f"""You are an AI skill evaluator for engineering students. Analyze this GitHub profile data and provide a comprehensive skill assessment.

GitHub Profile Data:
{json.dumps(github_data, indent=2, default=str)}

RESPOND IN VALID JSON ONLY (no markdown, no code blocks). Use this exact format:
{{
    "overall_score": <0-100>,
    "skills": [
        {{"name": "<skill_name>", "value": <0-100>, "category": "<Frontend|Backend|Mobile|AI/ML|DevOps|Database|Design|Other>"}},
        ...
    ],
    "strengths": ["<strength1>", "<strength2>", "<strength3>"],
    "growth_areas": ["<area1>", "<area2>"],
    "profile_summary": "<2-3 sentence summary of the developer's profile>",
    "recommended_to_learn": ["<skill1>", "<skill2>", "<skill3>"],
    "teaching_potential": ["<skill1>", "<skill2>"]
}}

Rules:
- Evaluate ALL detected programming languages and frameworks
- Score based on repo quality, quantity, activity, and diversity
- Be generous but realistic (students are learning)
- Include at least 5 skills
- Identify what they could teach others
- Suggest complementary skills to learn
"""
    
    try:
        response = gemini_model.generate_content(prompt)
        text = response.text.strip()
        # Clean up markdown code blocks if present
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
            text = text.rsplit('```', 1)[0]
        return json.loads(text)
    except Exception as e:
        print(f"Gemini AI error: {e}")
        return generate_fallback_analysis(github_data)

def generate_fallback_analysis(github_data):
    """Generate a reasonable analysis without AI."""
    languages = github_data.get('languages', {})
    total_bytes = sum(languages.values()) if languages else 1
    
    skills = []
    for lang, bytes_count in sorted(languages.items(), key=lambda x: x[1], reverse=True)[:10]:
        ratio = bytes_count / total_bytes
        value = min(95, max(20, int(ratio * 100 + 30)))
        
        category_map = {
            'JavaScript': 'Frontend', 'TypeScript': 'Frontend', 'HTML': 'Frontend', 'CSS': 'Frontend',
            'Python': 'Backend', 'Java': 'Backend', 'Go': 'Backend', 'Rust': 'Backend', 'C#': 'Backend',
            'Dart': 'Mobile', 'Swift': 'Mobile', 'Kotlin': 'Mobile',
            'Jupyter Notebook': 'AI/ML', 'R': 'AI/ML',
            'Dockerfile': 'DevOps', 'Shell': 'DevOps', 'HCL': 'DevOps',
            'SQL': 'Database', 'PLSQL': 'Database',
        }
        category = category_map.get(lang, 'Other')
        skills.append({'name': lang, 'value': value, 'category': category})
    
    repos = github_data.get('repos', [])
    activity = github_data.get('activity', {})
    
    overall = min(95, max(10, len(repos) * 3 + activity.get('push_events', 0) + len(skills) * 5))
    
    top_skills = [s['name'] for s in skills[:3]]
    
    return {
        'overall_score': overall,
        'skills': skills,
        'strengths': top_skills + ['Active GitHub contributor'],
        'growth_areas': ['Open source contributions', 'Documentation'],
        'profile_summary': f"Developer with experience in {', '.join(top_skills[:3])}. Active on GitHub with {len(repos)} repositories.",
        'recommended_to_learn': ['System Design', 'Testing', 'CI/CD'],
        'teaching_potential': top_skills[:2] if top_skills else ['Programming basics']
    }

# ─── Matching Algorithm ─────────────────────────────────────────────────

def calculate_match_score(user1, user2, user1_skills, user2_skills):
    """Calculate compatibility score between two users."""
    score = 0
    
    # Parse preferences
    u1_learn = json.loads(user1['want_to_learn']) if user1['want_to_learn'] else []
    u1_teach = json.loads(user1['can_teach']) if user1['can_teach'] else []
    u2_learn = json.loads(user2['want_to_learn']) if user2['want_to_learn'] else []
    u2_teach = json.loads(user2['can_teach']) if user2['can_teach'] else []
    
    # Skill complementarity (I want to learn what you can teach)
    u1_skill_names = {s['skill_name'].lower() for s in user1_skills}
    u2_skill_names = {s['skill_name'].lower() for s in user2_skills}
    
    # Check if user1's learning goals match user2's teaching abilities
    for skill in u1_learn:
        if skill.lower() in [t.lower() for t in u2_teach]:
            score += 25
        if skill.lower() in u2_skill_names:
            score += 10
    
    # Check reverse
    for skill in u2_learn:
        if skill.lower() in [t.lower() for t in u1_teach]:
            score += 25
        if skill.lower() in u1_skill_names:
            score += 10
    
    # Same city bonus
    if user1['city'].lower() == user2['city'].lower() and user1['city']:
        score += 15
    
    # Same college bonus
    if user1['college'].lower() == user2['college'].lower() and user1['college']:
        score += 10
    
    # Skill diversity bonus (different skill sets)
    overlap = u1_skill_names & u2_skill_names
    total = u1_skill_names | u2_skill_names
    if total:
        diversity = 1 - (len(overlap) / len(total))
        score += int(diversity * 15)
    
    return min(100, score)

def find_matches(user_id, limit=20):
    """Find best matching peers for a user."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    user_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (user_id,)).fetchall()
    
    # Get all other users
    other_users = db.execute('SELECT * FROM users WHERE id != ?', (user_id,)).fetchall()
    
    matches = []
    for other in other_users:
        other_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (other['id'],)).fetchall()
        match_score = calculate_match_score(user, other, user_skills, other_skills)
        
        if match_score > 0:
            matches.append({
                'user': dict(other),
                'skills': [dict(s) for s in other_skills],
                'match_score': match_score,
            })
    
    # Sort by match score
    matches.sort(key=lambda x: x['match_score'], reverse=True)
    return matches[:limit]

# ─── Routes: Auth ────────────────────────────────────────────────────────

@app.route('/')
def index():
    user = get_current_user()
    return render_template('index.html', user=user)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        college = request.form.get('college', '').strip()
        city = request.form.get('city', '').strip()
        
        if not all([name, email, password]):
            flash('Name, email and password are required.', 'error')
            return redirect(url_for('register'))
        
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        
        password_hash = generate_password_hash(password)
        db.execute(
            'INSERT INTO users (name, email, password_hash, college, city) VALUES (?, ?, ?, ?, ?)',
            (name, email, password_hash, college, city)
        )
        db.commit()
        
        user = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        session['user_id'] = user['id']
        flash('Welcome to SkillSwap! 🎉', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            flash('Welcome back! 👋', 'success')
            return redirect(url_for('dashboard'))
        
        flash('Invalid credentials.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))

# ─── Routes: Dashboard ──────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user = get_current_user()
    skills = db.execute('SELECT * FROM skills WHERE user_id = ? ORDER BY skill_value DESC', (session['user_id'],)).fetchall()
    
    # Stats
    total_sessions = db.execute('''
        SELECT COUNT(*) as count FROM sessions s 
        JOIN matches m ON s.match_id = m.id 
        WHERE (m.user1_id = ? OR m.user2_id = ?) AND s.status = 'completed'
    ''', (session['user_id'], session['user_id'])).fetchone()['count']
    
    total_matches = db.execute('''
        SELECT COUNT(*) as count FROM matches 
        WHERE (user1_id = ? OR user2_id = ?) AND status = 'accepted'
    ''', (session['user_id'], session['user_id'])).fetchone()['count']
    
    badges = db.execute('SELECT * FROM badges WHERE user_id = ?', (session['user_id'],)).fetchall()
    
    # Recent activity  
    recent_sessions = db.execute('''
        SELECT s.*, u.name as peer_name FROM sessions s 
        JOIN matches m ON s.match_id = m.id 
        JOIN users u ON u.id = CASE WHEN m.user1_id = ? THEN m.user2_id ELSE m.user1_id END
        WHERE m.user1_id = ? OR m.user2_id = ?
        ORDER BY s.created_at DESC LIMIT 5
    ''', (session['user_id'], session['user_id'], session['user_id'])).fetchall()
    
    return render_template('dashboard.html', user=user, skills=skills, 
                         total_sessions=total_sessions, total_matches=total_matches,
                         badges=badges, recent_sessions=recent_sessions)

# ─── Routes: Profile ────────────────────────────────────────────────────

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db = get_db()
    user = get_current_user()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        college = request.form.get('college', '').strip()
        city = request.form.get('city', '').strip()
        bio = request.form.get('bio', '').strip()
        github_username = request.form.get('github_username', '').strip()
        want_to_learn = request.form.get('want_to_learn', '').strip()
        can_teach = request.form.get('can_teach', '').strip()
        availability = request.form.get('availability', 'Flexible')
        
        # Parse comma-separated values to JSON arrays
        learn_list = [s.strip() for s in want_to_learn.split(',') if s.strip()]
        teach_list = [s.strip() for s in can_teach.split(',') if s.strip()]
        
        db.execute('''
            UPDATE users SET name=?, college=?, city=?, bio=?, github_username=?,
            want_to_learn=?, can_teach=?, availability=? WHERE id=?
        ''', (name, college, city, bio, github_username,
              json.dumps(learn_list), json.dumps(teach_list), availability, session['user_id']))
        db.commit()
        flash('Profile updated! ✅', 'success')
        return redirect(url_for('profile'))
    
    skills = db.execute('SELECT * FROM skills WHERE user_id = ? ORDER BY skill_value DESC', (session['user_id'],)).fetchall()
    return render_template('profile.html', user=user, skills=skills)

@app.route('/profile/<int:user_id>')
@login_required
def view_profile(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('marketplace'))
    
    skills = db.execute('SELECT * FROM skills WHERE user_id = ? ORDER BY skill_value DESC', (user_id,)).fetchall()
    reviews = db.execute('''
        SELECT r.*, u.name as reviewer_name FROM reviews r 
        JOIN users u ON r.reviewer_id = u.id
        JOIN sessions s ON r.session_id = s.id
        JOIN matches m ON s.match_id = m.id
        WHERE m.user1_id = ? OR m.user2_id = ?
        ORDER BY r.created_at DESC LIMIT 10
    ''', (user_id, user_id)).fetchall()
    badges = db.execute('SELECT * FROM badges WHERE user_id = ?', (user_id,)).fetchall()
    
    current_user = get_current_user()
    return render_template('view_profile.html', profile_user=user, skills=skills, 
                         reviews=reviews, badges=badges, user=current_user)

# ─── Routes: GitHub Analysis ────────────────────────────────────────────

@app.route('/analyze', methods=['GET', 'POST'])
@login_required
def analyze():
    user = get_current_user()
    analysis = None
    
    if request.method == 'POST':
        github_username = request.form.get('github_username', '').strip()
        if not github_username:
            flash('Please enter a GitHub username.', 'error')
            return redirect(url_for('analyze'))
        
        # Fetch GitHub data
        github_data = fetch_github_data(github_username)
        if not github_data:
            flash('Could not fetch GitHub data. Check the username.', 'error')
            return redirect(url_for('analyze'))
        
        # AI Analysis
        analysis = analyze_with_ai(github_data)
        analysis['github_data'] = github_data
        
        # Save to database
        db = get_db()
        
        # Update user's GitHub username and avatar
        avatar_url = github_data['profile'].get('avatar_url', '')
        db.execute('UPDATE users SET github_username=?, avatar_url=?, overall_score=? WHERE id=?',
                   (github_username, avatar_url, analysis.get('overall_score', 0), session['user_id']))
        
        # Clear old skills and insert new ones
        db.execute('DELETE FROM skills WHERE user_id = ? AND verified_via = ?', (session['user_id'], 'github'))
        for skill in analysis.get('skills', []):
            db.execute('INSERT INTO skills (user_id, skill_name, skill_value, category, verified_via) VALUES (?, ?, ?, ?, ?)',
                       (session['user_id'], skill['name'], skill['value'], skill.get('category', 'Other'), 'github'))
        
        # Update learning/teaching preferences from AI
        if analysis.get('recommended_to_learn'):
            current_learn = json.loads(user['want_to_learn']) if user['want_to_learn'] else []
            new_learn = list(set(current_learn + analysis['recommended_to_learn']))
            db.execute('UPDATE users SET want_to_learn = ? WHERE id = ?', (json.dumps(new_learn), session['user_id']))
        
        if analysis.get('teaching_potential'):
            current_teach = json.loads(user['can_teach']) if user['can_teach'] else []
            new_teach = list(set(current_teach + analysis['teaching_potential']))
            db.execute('UPDATE users SET can_teach = ? WHERE id = ?', (json.dumps(new_teach), session['user_id']))
        
        # Save full analysis
        db.execute('INSERT INTO github_analyses (user_id, github_username, analysis_data) VALUES (?, ?, ?)',
                   (session['user_id'], github_username, json.dumps(analysis, default=str)))
        
        # Award badge for first analysis
        existing_badge = db.execute('SELECT id FROM badges WHERE user_id = ? AND badge_name = ?', 
                                    (session['user_id'], 'GitHub Verified')).fetchone()
        if not existing_badge:
            db.execute('INSERT INTO badges (user_id, badge_name, badge_icon, description) VALUES (?, ?, ?, ?)',
                       (session['user_id'], 'GitHub Verified', '✅', 'Verified GitHub profile with AI analysis'))
        
        db.commit()
        flash('GitHub profile analyzed successfully! 🧠', 'success')
    
    # Get previous analyses
    db = get_db()
    past_analyses = db.execute('''
        SELECT * FROM github_analyses WHERE user_id = ? ORDER BY analyzed_at DESC LIMIT 5
    ''', (session['user_id'],)).fetchall()
    
    return render_template('analyze.html', user=user, analysis=analysis, past_analyses=past_analyses)

# ─── Routes: Marketplace ────────────────────────────────────────────────

@app.route('/marketplace')
@login_required
def marketplace():
    db = get_db()
    user = get_current_user()
    
    # Filters
    skill_filter = request.args.get('skill', '')
    city_filter = request.args.get('city', '')
    college_filter = request.args.get('college', '')
    
    query = 'SELECT * FROM users WHERE id != ?'
    params = [session['user_id']]
    
    if city_filter:
        query += ' AND LOWER(city) LIKE ?'
        params.append(f'%{city_filter.lower()}%')
    if college_filter:
        query += ' AND LOWER(college) LIKE ?'
        params.append(f'%{college_filter.lower()}%')
    
    query += ' ORDER BY overall_score DESC'
    all_users = db.execute(query, params).fetchall()
    
    # Enrich with skills
    profiles = []
    for u in all_users:
        skills = db.execute('SELECT * FROM skills WHERE user_id = ? ORDER BY skill_value DESC', (u['id'],)).fetchall()
        
        if skill_filter:
            has_skill = any(skill_filter.lower() in s['skill_name'].lower() for s in skills)
            if not has_skill:
                continue
        
        profiles.append({
            'user': dict(u),
            'skills': [dict(s) for s in skills[:6]],
            'want_to_learn': json.loads(u['want_to_learn']) if u['want_to_learn'] else [],
            'can_teach': json.loads(u['can_teach']) if u['can_teach'] else [],
        })
    
    # Get unique cities and colleges for filters
    cities = db.execute('SELECT DISTINCT city FROM users WHERE city != "" ORDER BY city').fetchall()
    colleges = db.execute('SELECT DISTINCT college FROM users WHERE college != "" ORDER BY college').fetchall()
    
    return render_template('marketplace.html', user=user, profiles=profiles, 
                         cities=cities, colleges=colleges,
                         skill_filter=skill_filter, city_filter=city_filter, college_filter=college_filter)

# ─── Routes: Matching ───────────────────────────────────────────────────

@app.route('/matches')
@login_required
def matches():
    db = get_db()
    user = get_current_user()
    
    # AI-suggested matches
    suggested = find_matches(session['user_id'])
    
    # Existing matches
    existing = db.execute('''
        SELECT m.*, 
            u1.name as user1_name, u1.avatar_url as user1_avatar,
            u2.name as user2_name, u2.avatar_url as user2_avatar
        FROM matches m
        JOIN users u1 ON m.user1_id = u1.id
        JOIN users u2 ON m.user2_id = u2.id
        WHERE m.user1_id = ? OR m.user2_id = ?
        ORDER BY m.created_at DESC
    ''', (session['user_id'], session['user_id'])).fetchall()
    
    return render_template('matches.html', user=user, suggested=suggested, existing=existing)

@app.route('/match/request/<int:target_id>', methods=['POST'])
@login_required
def request_match(target_id):
    db = get_db()
    
    # Check if match already exists
    existing = db.execute('''
        SELECT id FROM matches WHERE 
        (user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)
    ''', (session['user_id'], target_id, target_id, session['user_id'])).fetchone()
    
    if existing:
        flash('Match already exists!', 'info')
        return redirect(url_for('matches'))
    
    # Calculate match score
    user = get_current_user()
    target = db.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
    user_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (session['user_id'],)).fetchall()
    target_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (target_id,)).fetchall()
    
    match_score = calculate_match_score(user, target, user_skills, target_skills)
    
    db.execute('INSERT INTO matches (user1_id, user2_id, match_score, status) VALUES (?, ?, ?, ?)',
               (session['user_id'], target_id, match_score, 'pending'))
    db.commit()
    
    flash(f'Match request sent to {target["name"]}! 🤝', 'success')
    return redirect(url_for('matches'))

@app.route('/match/respond/<int:match_id>/<action>', methods=['POST'])
@login_required
def respond_match(match_id, action):
    db = get_db()
    match = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    
    if not match or match['user2_id'] != session['user_id']:
        flash('Invalid match.', 'error')
        return redirect(url_for('matches'))
    
    if action == 'accept':
        db.execute('UPDATE matches SET status = ? WHERE id = ?', ('accepted', match_id))
        db.commit()
        flash('Match accepted! You can now start a learning session. 🎉', 'success')
    elif action == 'decline':
        db.execute('UPDATE matches SET status = ? WHERE id = ?', ('declined', match_id))
        db.commit()
        flash('Match declined.', 'info')
    
    return redirect(url_for('matches'))

# ─── Routes: Sessions ───────────────────────────────────────────────────

@app.route('/sessions')
@login_required
def sessions():
    db = get_db()
    user = get_current_user()
    
    user_sessions = db.execute('''
        SELECT s.*, m.user1_id, m.user2_id,
            u.name as peer_name, u.avatar_url as peer_avatar
        FROM sessions s
        JOIN matches m ON s.match_id = m.id
        JOIN users u ON u.id = CASE WHEN m.user1_id = ? THEN m.user2_id ELSE m.user1_id END
        WHERE m.user1_id = ? OR m.user2_id = ?
        ORDER BY s.created_at DESC
    ''', (session['user_id'], session['user_id'], session['user_id'])).fetchall()
    
    # Get accepted matches for new session creation
    accepted_matches = db.execute('''
        SELECT m.*, u.name as peer_name FROM matches m
        JOIN users u ON u.id = CASE WHEN m.user1_id = ? THEN m.user2_id ELSE m.user1_id END
        WHERE (m.user1_id = ? OR m.user2_id = ?) AND m.status = 'accepted'
    ''', (session['user_id'], session['user_id'], session['user_id'])).fetchall()
    
    return render_template('sessions.html', user=user, sessions=user_sessions, accepted_matches=accepted_matches)

@app.route('/session/create', methods=['POST'])
@login_required
def create_session():
    db = get_db()
    match_id = request.form.get('match_id')
    topic = request.form.get('topic', '').strip()
    scheduled_at = request.form.get('scheduled_at', '')
    duration = request.form.get('duration', 30)
    
    if not all([match_id, topic]):
        flash('Please fill all required fields.', 'error')
        return redirect(url_for('sessions'))
    
    db.execute('''
        INSERT INTO sessions (match_id, requester_id, topic, scheduled_at, duration_mins, status)
        VALUES (?, ?, ?, ?, ?, 'requested')
    ''', (match_id, session['user_id'], topic, scheduled_at, duration))
    db.commit()
    
    flash('Session requested! ⏰', 'success')
    return redirect(url_for('sessions'))

@app.route('/session/<int:session_id>')
@login_required
def view_session(session_id):
    db = get_db()
    user = get_current_user()
    
    sess = db.execute('''
        SELECT s.*, m.user1_id, m.user2_id,
            u.name as peer_name, u.avatar_url as peer_avatar
        FROM sessions s
        JOIN matches m ON s.match_id = m.id
        JOIN users u ON u.id = CASE WHEN m.user1_id = ? THEN m.user2_id ELSE m.user1_id END
        WHERE s.id = ? AND (m.user1_id = ? OR m.user2_id = ?)
    ''', (session['user_id'], session_id, session['user_id'], session['user_id'])).fetchone()
    
    if not sess:
        flash('Session not found.', 'error')
        return redirect(url_for('sessions'))
    
    messages = db.execute('''
        SELECT msg.*, u.name as sender_name, u.avatar_url as sender_avatar
        FROM messages msg
        JOIN users u ON msg.sender_id = u.id
        WHERE msg.session_id = ?
        ORDER BY msg.sent_at ASC
    ''', (session_id,)).fetchall()
    
    # Check if review exists
    existing_review = db.execute('SELECT id FROM reviews WHERE session_id = ? AND reviewer_id = ?',
                                  (session_id, session['user_id'])).fetchone()
    
    return render_template('session_detail.html', user=user, session=sess, 
                         messages=messages, existing_review=existing_review)

@app.route('/session/<int:session_id>/respond/<action>', methods=['POST'])
@login_required
def respond_session(session_id, action):
    db = get_db()
    if action == 'accept':
        db.execute('UPDATE sessions SET status = ? WHERE id = ?', ('accepted', session_id))
        flash('Session accepted! 🎉', 'success')
    elif action == 'decline':
        db.execute('UPDATE sessions SET status = ? WHERE id = ?', ('declined', session_id))
        flash('Session declined.', 'info')
    elif action == 'complete':
        db.execute('UPDATE sessions SET status = ? WHERE id = ?', ('completed', session_id))
        # Award badge
        sess = db.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
        match = db.execute('SELECT * FROM matches WHERE id = ?', (sess['match_id'],)).fetchone()
        for uid in [match['user1_id'], match['user2_id']]:
            count = db.execute('''
                SELECT COUNT(*) as c FROM sessions s JOIN matches m ON s.match_id = m.id
                WHERE (m.user1_id = ? OR m.user2_id = ?) AND s.status = 'completed'
            ''', (uid, uid)).fetchone()['c']
            if count == 1:
                db.execute('INSERT INTO badges (user_id, badge_name, badge_icon, description) VALUES (?, ?, ?, ?)',
                           (uid, 'First Session', '🎓', 'Completed first learning session'))
            if count == 5:
                db.execute('INSERT INTO badges (user_id, badge_name, badge_icon, description) VALUES (?, ?, ?, ?)',
                           (uid, 'Skill Sharer', '🌟', 'Completed 5 learning sessions'))
        flash('Session completed! Don\'t forget to leave a review. ⭐', 'success')
    db.commit()
    return redirect(url_for('view_session', session_id=session_id))

# ─── Routes: Chat ───────────────────────────────────────────────────────

@app.route('/session/<int:session_id>/send', methods=['POST'])
@login_required
def send_message(session_id):
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Empty message'}), 400
    
    db = get_db()
    db.execute('INSERT INTO messages (session_id, sender_id, content) VALUES (?, ?, ?)',
               (session_id, session['user_id'], content))
    db.commit()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})
    return redirect(url_for('view_session', session_id=session_id))

@app.route('/session/<int:session_id>/messages')
@login_required
def get_messages(session_id):
    db = get_db()
    after = request.args.get('after', 0, type=int)
    
    messages = db.execute('''
        SELECT msg.*, u.name as sender_name, u.avatar_url as sender_avatar
        FROM messages msg
        JOIN users u ON msg.sender_id = u.id
        WHERE msg.session_id = ? AND msg.id > ?
        ORDER BY msg.sent_at ASC
    ''', (session_id, after)).fetchall()
    
    return jsonify([{
        'id': m['id'],
        'sender_id': m['sender_id'],
        'sender_name': m['sender_name'],
        'sender_avatar': m['sender_avatar'] or '',
        'content': m['content'],
        'sent_at': m['sent_at'],
        'is_mine': m['sender_id'] == session['user_id'],
    } for m in messages])

# ─── Routes: Reviews ────────────────────────────────────────────────────

@app.route('/session/<int:session_id>/review', methods=['POST'])
@login_required
def submit_review(session_id):
    rating = request.form.get('rating', 5, type=int)
    comment = request.form.get('comment', '').strip()
    
    db = get_db()
    existing = db.execute('SELECT id FROM reviews WHERE session_id = ? AND reviewer_id = ?',
                          (session_id, session['user_id'])).fetchone()
    if existing:
        flash('You already reviewed this session.', 'info')
    else:
        db.execute('INSERT INTO reviews (session_id, reviewer_id, rating, comment) VALUES (?, ?, ?, ?)',
                   (session_id, session['user_id'], rating, comment))
        db.commit()
        flash('Review submitted! Thank you! ⭐', 'success')
    
    return redirect(url_for('view_session', session_id=session_id))

# ─── Routes: Leaderboard ────────────────────────────────────────────────

@app.route('/leaderboard')
@login_required
def leaderboard():
    db = get_db()
    user = get_current_user()
    
    city_filter = request.args.get('city', '')
    
    query = '''
        SELECT u.*, COUNT(DISTINCT s.id) as session_count,
            AVG(r.rating) as avg_rating,
            COUNT(DISTINCT b.id) as badge_count
        FROM users u
        LEFT JOIN matches m ON (m.user1_id = u.id OR m.user2_id = u.id) AND m.status = 'accepted'
        LEFT JOIN sessions s ON s.match_id = m.id AND s.status = 'completed'
        LEFT JOIN reviews r ON r.session_id = s.id AND r.reviewer_id != u.id
        LEFT JOIN badges b ON b.user_id = u.id
    '''
    params = []
    
    if city_filter:
        query += ' WHERE LOWER(u.city) LIKE ?'
        params.append(f'%{city_filter.lower()}%')
    
    query += ' GROUP BY u.id ORDER BY u.overall_score DESC, session_count DESC LIMIT 50'
    
    leaders = db.execute(query, params).fetchall()
    cities = db.execute('SELECT DISTINCT city FROM users WHERE city != "" ORDER BY city').fetchall()
    
    return render_template('leaderboard.html', user=user, leaders=leaders, 
                         cities=cities, city_filter=city_filter)

# ─── API Endpoints ──────────────────────────────────────────────────────

@app.route('/api/skills/<int:user_id>')
@login_required
def api_skills(user_id):
    db = get_db()
    skills = db.execute('SELECT skill_name, skill_value, category FROM skills WHERE user_id = ? ORDER BY skill_value DESC', 
                        (user_id,)).fetchall()
    return jsonify([dict(s) for s in skills])

@app.route('/api/match-score/<int:target_id>')
@login_required
def api_match_score(target_id):
    db = get_db()
    user = get_current_user()
    target = db.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
    if not target:
        return jsonify({'error': 'User not found'}), 404
    
    user_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (session['user_id'],)).fetchall()
    target_skills = db.execute('SELECT * FROM skills WHERE user_id = ?', (target_id,)).fetchall()
    
    score = calculate_match_score(user, target, user_skills, target_skills)
    return jsonify({'match_score': score})

# ─── Error Handlers ─────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500

# ─── Main ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)

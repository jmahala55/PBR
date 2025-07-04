from flask import Flask, render_template, jsonify, request
from google.cloud import bigquery
import os
import json
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import io

# NEW IMPORTS FOR WEASYPRINT
import weasyprint
from jinja2 import Template

app = Flask(__name__)

# Load email configuration from file
def load_email_config():
    try:
        with open('email_config.json', 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print("email_config.json not found. Please create it with your email credentials.")
        return None
    except json.JSONDecodeError:
        print("Error parsing email_config.json. Please check the JSON format.")
        return None

# Load email config
email_config = load_email_config()

# Email configuration from file
if email_config:
    EMAIL_HOST = email_config.get('host', 'smtp.gmail.com')
    EMAIL_PORT = email_config.get('port', 587)
    EMAIL_USERNAME = email_config.get('username', '')
    EMAIL_PASSWORD = email_config.get('password', '')
    EMAIL_FROM = email_config.get('from', email_config.get('username', ''))
else:
    # Default values if config file is not available
    EMAIL_HOST = 'smtp.gmail.com'
    EMAIL_PORT = 587
    EMAIL_USERNAME = ''
    EMAIL_PASSWORD = ''
    EMAIL_FROM = ''

# Set up Google Cloud credentials
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'harvard-baseball-13fab221b2d4.json'

# Initialize BigQuery client
try:
    client = bigquery.Client()
    print("BigQuery client initialized successfully")
except Exception as e:
    print(f"Error initializing BigQuery client: {e}")
    client = None

def get_college_averages(pitch_type, comparison_level='D1', pitcher_throws='Right'):
    """Get college baseball averages for comparison, filtered by pitcher handedness"""
    try:
        # Determine the WHERE clause based on comparison level
        if comparison_level == 'SEC':
            level_filter = "League = 'SEC'"
        elif comparison_level in ['D1', 'D2', 'D3']:
            level_filter = f"Level = '{comparison_level}'"
        else:
            level_filter = "Level = 'D1'"  # Default to D1
        
        query = f"""
        SELECT 
            AVG(RelSpeed) as avg_velocity,
            AVG(SpinRate) as avg_spin_rate,
            AVG(InducedVertBreak) as avg_ivb,
            AVG(HorzBreak) as avg_hb,
            AVG(RelSide) as avg_rel_side,
            AVG(RelHeight) as avg_rel_height,
            AVG(Extension) as avg_extension,
            COUNT(*) as pitch_count
        FROM `NCAABaseball.2025Final`
        WHERE TaggedPitchType = @pitch_type
        AND {level_filter}
        AND PitcherThrows = @pitcher_throws
        AND RelSpeed IS NOT NULL
        AND SpinRate IS NOT NULL
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pitch_type", "STRING", pitch_type),
                bigquery.ScalarQueryParameter("pitcher_throws", "STRING", pitcher_throws),
            ]
        )
        
        result = client.query(query, job_config=job_config)
        row = list(result)[0] if result else None
        
        if row and row.pitch_count > 0:
            return {
                'avg_velocity': float(row.avg_velocity) if row.avg_velocity else None,
                'avg_spin_rate': float(row.avg_spin_rate) if row.avg_spin_rate else None,
                'avg_ivb': float(row.avg_ivb) if row.avg_ivb else None,
                'avg_hb': float(row.avg_hb) if row.avg_hb else None,
                'avg_rel_side': float(row.avg_rel_side) if row.avg_rel_side else None,
                'avg_rel_height': float(row.avg_rel_height) if row.avg_rel_height else None,
                'avg_extension': float(row.avg_extension) if row.avg_extension else None,
                'pitch_count': int(row.pitch_count)
            }
        return None
        
    except Exception as e:
        print(f"Error getting college averages for {pitch_type} ({pitcher_throws}): {str(e)}")
        return None

def calculate_percentile(value, comparison_value, metric_name=None, pitch_type=None, pitcher_throws=None):
    """Calculate how the pitcher's value compares to college average with proper pitch-specific logic"""
    if value is None or comparison_value is None:
        return None
    
    difference = value - comparison_value
    percentage_diff = (difference / comparison_value) * 100
    
    # Determine if the difference is "better" based on metric and pitch type
    if metric_name == 'hb' and pitch_type and pitcher_throws:
        better = is_horizontal_break_better(difference, pitch_type, pitcher_throws)
    elif metric_name == 'ivb' and pitch_type:
        better = is_ivb_better(difference, pitch_type)
    elif metric_name == 'velocity' and pitch_type:
        better = is_velocity_better(difference, pitch_type)
    else:
        # For all other metrics, more is generally better
        better = difference > 0
    
    return {
        'difference': difference,
        'percentage_diff': percentage_diff,
        'better': better
    }

@app.route('/')
def index():
    """Serve the main HTML page"""
    return render_template('index.html')

@app.route('/api/dates')
def get_dates():
    """API endpoint to get all available dates"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    try:
        query = """
        SELECT DISTINCT Date
        FROM `V1PBR.Test`
        WHERE Date IS NOT NULL
        ORDER BY Date
        """
        
        result = client.query(query)
        dates = []
        for row in result:
            # Convert date to string format that matches what's stored
            date_val = row.Date
            if hasattr(date_val, 'strftime'):
                # If it's a datetime object, format it
                dates.append(date_val.strftime('%Y-%m-%d'))
            else:
                # If it's already a string, use as-is
                dates.append(str(date_val))
        
        return jsonify({'dates': dates})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/pitchers')
def get_pitchers():
    """API endpoint to get unique pitchers for a specific date"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify({'error': 'Date parameter is required'}), 400
    
    try:
        # First, let's check what the actual date format is in the table
        debug_query = """
        SELECT DISTINCT Date, TYPEOF(Date) as date_type
        FROM `V1PBR.Test`
        WHERE Date IS NOT NULL
        LIMIT 5
        """
        
        debug_result = client.query(debug_query)
        print("Debug - Date formats in table:")
        for row in debug_result:
            print(f"Date: {row.Date}, Type: {row.date_type}")
        
        # Try different date matching approaches
        query = """
        SELECT DISTINCT Pitcher
        FROM `V1PBR.Test`
        WHERE CAST(Date AS STRING) = @date
        AND Pitcher IS NOT NULL
        ORDER BY Pitcher
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "STRING", selected_date),
            ]
        )
        
        result = client.query(query, job_config=job_config)
        pitchers = [row.Pitcher for row in result]
        
        return jsonify({'pitchers': pitchers})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/pitcher-details')
def get_pitcher_details():
    """API endpoint to get detailed pitch data for a specific pitcher and date"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    selected_date = request.args.get('date')
    pitcher_name = request.args.get('pitcher')
    
    if not selected_date or not pitcher_name:
        return jsonify({'error': 'Date and pitcher parameters are required'}), 400
    
    try:
        query = """
        SELECT *
        FROM `V1PBR.Test`
        WHERE CAST(Date AS STRING) = @date
        AND Pitcher = @pitcher
        ORDER BY PitchNo
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "STRING", selected_date),
                bigquery.ScalarQueryParameter("pitcher", "STRING", pitcher_name),
            ]
        )
        
        result = client.query(query, job_config=job_config)
        
        # Convert to list of dictionaries
        pitch_data = []
        for row in result:
            pitch_data.append(dict(row))
        
        return jsonify({'pitch_data': pitch_data})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats')
def get_stats():
    """API endpoint to get general dataset statistics"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    try:
        # Get total record count
        count_query = "SELECT COUNT(*) as total FROM `V1PBR.Test`"
        count_result = client.query(count_query)
        total_records = list(count_result)[0].total
        
        # Get date range
        date_range_query = """
        SELECT 
            MIN(CAST(Date AS STRING)) as earliest_date,
            MAX(CAST(Date AS STRING)) as latest_date,
            COUNT(DISTINCT CAST(Date AS STRING)) as unique_dates,
            COUNT(DISTINCT Pitcher) as unique_pitchers
        FROM `V1PBR.Test`
        WHERE Date IS NOT NULL
        """
        
        date_result = client.query(date_range_query)
        date_info = list(date_result)[0]
        
        # Get matching analysis between Test and Info tables
        # Get all pitchers from Test table
        test_pitchers_query = """
        SELECT DISTINCT Pitcher
        FROM `V1PBR.Test`
        WHERE Pitcher IS NOT NULL
        ORDER BY Pitcher
        """
        
        test_result = client.query(test_pitchers_query)
        test_pitchers = set([row.Pitcher for row in test_result])
        
        # Get all prospects from Info table
        info_prospects_query = """
        SELECT Event, Prospect, Email, Type
        FROM `V1PBRInfo.Info`
        ORDER BY Prospect
        """
        
        info_result = client.query(info_prospects_query)
        info_prospects = []
        info_prospect_names = set()
        
        for row in info_result:
            info_prospects.append({
                'name': row.Prospect,
                'email': row.Email,
                'type': row.Type,
                'event': row.Event
            })
            info_prospect_names.add(row.Prospect)
        
        # Find matches and mismatches
        matched_names = test_pitchers.intersection(info_prospect_names)
        test_only = test_pitchers - info_prospect_names  # In Test but not in Info
        info_only = info_prospect_names - test_pitchers  # In Info but not in Test
        
        # Get email info for matched prospects
        matched_with_email = 0
        matched_without_email = 0
        
        for prospect in info_prospects:
            if prospect['name'] in matched_names:
                if prospect['email']:
                    matched_with_email += 1
                else:
                    matched_without_email += 1
        
        return jsonify({
            'total_records': total_records,
            'earliest_date': date_info.earliest_date,
            'latest_date': date_info.latest_date,
            'unique_dates': date_info.unique_dates,
            'unique_pitchers': date_info.unique_pitchers,
            'matching_stats': {
                'total_in_info': len(info_prospect_names),
                'total_in_test': len(test_pitchers),
                'matched_names': len(matched_names),
                'matched_with_email': matched_with_email,
                'matched_without_email': matched_without_email,
                'in_test_only': len(test_only),
                'in_info_only': len(info_only),
                'test_only_names': list(test_only),
                'info_only_names': list(info_only),
                'matched_names_list': list(matched_names)
            }
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Add this new function to get college max velocity averages
def get_college_max_velocity_averages(pitch_type, comparison_level='D1', pitcher_throws='Right'):
    """Get college baseball MAX velocity averages for comparison, filtered by pitcher handedness"""
    try:
        # Determine the WHERE clause based on comparison level
        if comparison_level == 'SEC':
            level_filter = "League = 'SEC'"
        elif comparison_level in ['D1', 'D2', 'D3']:
            level_filter = f"Level = '{comparison_level}'"
        else:
            level_filter = "Level = 'D1'"  # Default to D1
        
        query = f"""
        SELECT 
            AVG(max_velo) as avg_max_velocity,
            COUNT(DISTINCT Pitcher) as pitcher_count
        FROM (
            SELECT 
                Pitcher,
                MAX(RelSpeed) as max_velo
            FROM `NCAABaseball.2025Final`
            WHERE TaggedPitchType = @pitch_type
            AND {level_filter}
            AND PitcherThrows = @pitcher_throws
            AND RelSpeed IS NOT NULL
            GROUP BY Pitcher
        )
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pitch_type", "STRING", pitch_type),
                bigquery.ScalarQueryParameter("pitcher_throws", "STRING", pitcher_throws),
            ]
        )
        
        result = client.query(query, job_config=job_config)
        row = list(result)[0] if result else None
        
        if row and row.pitcher_count > 0:
            return {
                'avg_max_velocity': float(row.avg_max_velocity) if row.avg_max_velocity else None,
                'pitcher_count': int(row.pitcher_count)
            }
        return None
        
    except Exception as e:
        print(f"Error getting college max velocity averages for {pitch_type} ({pitcher_throws}): {str(e)}")
        return None

# Replace the calculate_percentile_rank function with this new function:

def calculate_difference_from_average(pitcher_value, college_average, metric_name=None, pitch_type=None, pitcher_throws=None):
    """Calculate the difference between pitcher's value and college average"""
    if pitcher_value is None or college_average is None:
        return None
    
    difference = pitcher_value - college_average
    
    # Determine if the difference is "better" based on metric and pitch type
    if metric_name == 'hb' and pitch_type and pitcher_throws:
        better = is_horizontal_break_better(difference, pitch_type, pitcher_throws)
    elif metric_name == 'ivb' and pitch_type:
        better = is_ivb_better(difference, pitch_type)
    elif metric_name == 'velocity' and pitch_type:
        better = is_velocity_better(difference, pitch_type)
    else:
        # For all other metrics, more is generally better
        better = difference > 0
    
    return {
        'difference': difference,
        'better': better,
        'absolute_diff': abs(difference)
    }

def is_ivb_better(difference, pitch_type):
    """Determine if IVB difference is better based on pitch type"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories where negative IVB is better
    negative_ivb_pitches = ['curveball', 'curve', 'changeup', 'change-up', 'splitter', 'split-finger']
    
    is_negative_ivb_pitch = any(neg_pitch in pitch_type_lower for neg_pitch in negative_ivb_pitches)
    
    if is_negative_ivb_pitch:
        # For these pitches, more negative IVB is better
        return difference < 0
    else:
        # For all other pitches (fastballs, sliders, cutters), more positive IVB is better
        return difference > 0

def is_velocity_better(difference, pitch_type):
    """Determine if velocity difference is better based on pitch type"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories where lower velocity is better
    lower_velo_pitches = ['changeup', 'change-up', 'splitter', 'split-finger']
    
    is_lower_velo_pitch = any(low_pitch in pitch_type_lower for low_pitch in lower_velo_pitches)
    
    if is_lower_velo_pitch:
        # For changeups and splitters, lower velocity is better (more separation from fastball)
        return difference < 0
    else:
        # For all other pitches, higher velocity is better
        return difference > 0

def is_horizontal_break_better(difference, pitch_type, pitcher_throws):
    """Determine if horizontal break difference is better based on pitch type and handedness"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories
    breaking_balls = ['curveball', 'curve', 'slider', 'cutter', 'cut fastball']
    fastballs_and_offspeed = ['fastball', 'four-seam', '4-seam', 'sinker', 'two-seam', '2-seam', 
                              'changeup', 'change-up', 'splitter', 'split-finger', 'knuckleball']
    
    # Determine pitch category
    is_breaking_ball = any(bb in pitch_type_lower for bb in breaking_balls)
    is_fastball_or_offspeed = any(fo in pitch_type_lower for fo in fastballs_and_offspeed)
    
    if pitcher_throws == 'Right':
        if is_breaking_ball:
            # RHP breaking balls should go negative (more negative is better)
            return difference < 0
        elif is_fastball_or_offspeed:
            # RHP fastballs/offspeed should go positive (more positive is better)
            return difference > 0
    elif pitcher_throws == 'Left':
        if is_breaking_ball:
            # LHP breaking balls should go positive (more positive is better)
            return difference > 0
        elif is_fastball_or_offspeed:
            # LHP fastballs/offspeed should go negative (more negative is better)
            return difference < 0
    
    # Default case: if pitch type doesn't match known categories, assume more is better
    return difference > 0

# Update the get_multi_level_comparisons function:

def get_multi_level_comparisons(pitch_data, pitcher_throws='Right'):
    """Get comparisons across D1, D2, and D3 levels for all pitch types using plus/minus differences"""
    try:
        # Group pitches by type
        pitch_type_data = {}
        
        for pitch in pitch_data:
            pitch_type = pitch.get('TaggedPitchType', 'Unknown')
            if pitch_type not in pitch_type_data:
                pitch_type_data[pitch_type] = {
                    'pitches': [],
                    'count': 0
                }
            pitch_type_data[pitch_type]['pitches'].append(pitch)
            pitch_type_data[pitch_type]['count'] += 1
        
        # Calculate averages for each pitch type across all levels
        multi_level_breakdown = []
        levels = ['D1', 'D2', 'D3']
        
        # Define priority order - Fastball/Sinker first, then alphabetical
        priority_types = ['Fastball', 'Sinker', 'Four-Seam', '4-Seam', 'TwoSeam', 'Two-Seam']
        
        # Sort pitch types with priority
        sorted_pitch_types = []
        
        for priority_type in priority_types:
            for actual_type in pitch_type_data.keys():
                if priority_type.lower() in actual_type.lower() and actual_type not in sorted_pitch_types:
                    sorted_pitch_types.append(actual_type)
                    break
        
        remaining_types = [pt for pt in pitch_type_data.keys() if pt not in sorted_pitch_types]
        sorted_pitch_types.extend(sorted(remaining_types))
        
        for pitch_type in sorted_pitch_types:
            pitches = pitch_type_data[pitch_type]['pitches']
            count = pitch_type_data[pitch_type]['count']
            
            # Extract metrics
            velocities = [p.get('RelSpeed', 0) for p in pitches if p.get('RelSpeed')]
            spin_rates = [p.get('SpinRate', 0) for p in pitches if p.get('SpinRate')]
            ivbs = [p.get('InducedVertBreak', 0) for p in pitches if p.get('InducedVertBreak')]
            hbs = [p.get('HorzBreak', 0) for p in pitches if p.get('HorzBreak')]
            rel_heights = [p.get('RelHeight', 0) for p in pitches if p.get('RelHeight')]
            rel_sides = [p.get('RelSide', 0) for p in pitches if p.get('RelSide')]
            extensions = [p.get('Extension', 0) for p in pitches if p.get('Extension')]
            
            # Calculate averages AND max velocity
            pitcher_avg_velocity = sum(velocities)/len(velocities) if velocities else None
            pitcher_max_velocity = max(velocities) if velocities else None
            pitcher_avg_spin = sum(spin_rates)/len(spin_rates) if spin_rates else None
            pitcher_avg_ivb = sum(ivbs)/len(ivbs) if ivbs else None
            pitcher_avg_hb = sum(hbs)/len(hbs) if hbs else None
            pitcher_avg_rel_height = sum(rel_heights)/len(rel_heights) if rel_heights else None
            pitcher_avg_rel_side = sum(rel_sides)/len(rel_sides) if rel_sides else None
            pitcher_avg_extension = sum(extensions)/len(extensions) if extensions else None
            
            level_comparisons = {}
            
            for level in levels:
                # Get college averages for display
                college_averages = get_college_averages(pitch_type, level, pitcher_throws)
                college_max_velo_averages = get_college_max_velocity_averages(pitch_type, level, pitcher_throws)
                
                # Calculate differences from averages for each metric
                velocity_diff = calculate_difference_from_average(
                    pitcher_avg_velocity, 
                    college_averages['avg_velocity'] if college_averages else None,
                    metric_name='velocity',
                    pitch_type=pitch_type
                )
                
                max_velocity_diff = calculate_difference_from_average(
                    pitcher_max_velocity, 
                    college_max_velo_averages['avg_max_velocity'] if college_max_velo_averages else None,
                    metric_name='velocity',
                    pitch_type=pitch_type
                )
                
                spin_diff = calculate_difference_from_average(
                    pitcher_avg_spin, 
                    college_averages['avg_spin_rate'] if college_averages else None
                )
                
                ivb_diff = calculate_difference_from_average(
                    pitcher_avg_ivb, 
                    college_averages['avg_ivb'] if college_averages else None,
                    metric_name='ivb',
                    pitch_type=pitch_type
                )
                
                hb_diff = calculate_difference_from_average(
                    pitcher_avg_hb, 
                    college_averages['avg_hb'] if college_averages else None,
                    metric_name='hb',
                    pitch_type=pitch_type,
                    pitcher_throws=pitcher_throws
                )
                
                rel_height_diff = calculate_difference_from_average(
                    pitcher_avg_rel_height, 
                    college_averages['avg_rel_height'] if college_averages else None
                )
                
                rel_side_diff = calculate_difference_from_average(
                    pitcher_avg_rel_side, 
                    college_averages['avg_rel_side'] if college_averages else None
                )
                
                extension_diff = calculate_difference_from_average(
                    pitcher_avg_extension, 
                    college_averages['avg_extension'] if college_averages else None
                )
                
                level_comparisons[level] = {
                    'velocity': {
                        'college_avg': f"{college_averages['avg_velocity']:.1f}" if college_averages and college_averages['avg_velocity'] else 'N/A',
                        'comparison': velocity_diff,
                        'difference': f"{velocity_diff['difference']:+.1f}" if velocity_diff else 'N/A'
                    },
                    'max_velocity': {
                        'college_avg': f"{college_max_velo_averages['avg_max_velocity']:.1f}" if college_max_velo_averages and college_max_velo_averages['avg_max_velocity'] else 'N/A',
                        'comparison': max_velocity_diff,
                        'difference': f"{max_velocity_diff['difference']:+.1f}" if max_velocity_diff else 'N/A'
                    },
                    'spin': {
                        'college_avg': f"{college_averages['avg_spin_rate']:.0f}" if college_averages and college_averages['avg_spin_rate'] else 'N/A',
                        'comparison': spin_diff,
                        'difference': f"{spin_diff['difference']:+.0f}" if spin_diff else 'N/A'
                    },
                    'ivb': {
                        'college_avg': f"{college_averages['avg_ivb']:.1f}" if college_averages and college_averages['avg_ivb'] else 'N/A',
                        'comparison': ivb_diff,
                        'difference': f"{ivb_diff['difference']:+.1f}" if ivb_diff else 'N/A'
                    },
                    'hb': {
                        'college_avg': f"{college_averages['avg_hb']:.1f}" if college_averages and college_averages['avg_hb'] else 'N/A',
                        'comparison': hb_diff,
                        'difference': f"{hb_diff['difference']:+.1f}" if hb_diff else 'N/A'
                    },
                    'rel_height': {
                        'college_avg': f"{college_averages['avg_rel_height']:.1f}" if college_averages and college_averages['avg_rel_height'] else 'N/A',
                        'comparison': rel_height_diff,
                        'difference': f"{rel_height_diff['difference']:+.1f}" if rel_height_diff else 'N/A'
                    },
                    'rel_side': {
                        'college_avg': f"{college_averages['avg_rel_side']:.1f}" if college_averages and college_averages['avg_rel_side'] else 'N/A',
                        'comparison': rel_side_diff,
                        'difference': f"{rel_side_diff['difference']:+.1f}" if rel_side_diff else 'N/A'
                    },
                    'extension': {
                        'college_avg': f"{college_averages['avg_extension']:.1f}" if college_averages and college_averages['avg_extension'] else 'N/A',
                        'comparison': extension_diff,
                        'difference': f"{extension_diff['difference']:+.1f}" if extension_diff else 'N/A'
                    }
                }
            
            multi_level_breakdown.append({
                'name': pitch_type,
                'count': count,
                'pitcher_velocity': f"{pitcher_avg_velocity:.1f}" if pitcher_avg_velocity else 'N/A',
                'pitcher_max_velocity': f"{pitcher_max_velocity:.1f}" if pitcher_max_velocity else 'N/A',
                'pitcher_spin': f"{pitcher_avg_spin:.0f}" if pitcher_avg_spin else 'N/A',
                'pitcher_ivb': f"{pitcher_avg_ivb:.1f}" if pitcher_avg_ivb else 'N/A',
                'pitcher_hb': f"{pitcher_avg_hb:.1f}" if pitcher_avg_hb else 'N/A',
                'pitcher_rel_height': f"{pitcher_avg_rel_height:.1f}" if pitcher_avg_rel_height else 'N/A',
                'pitcher_rel_side': f"{pitcher_avg_rel_side:.1f}" if pitcher_avg_rel_side else 'N/A',
                'pitcher_extension': f"{pitcher_avg_extension:.1f}" if pitcher_avg_extension else 'N/A',
                'level_comparisons': level_comparisons
            })
        
        return multi_level_breakdown

    except Exception as e:
        print(f"Error getting multi-level comparisons: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


def generate_pitcher_pdf(pitcher_name, pitch_data, date, comparison_level='D1'):
    """Generate a PDF report for the pitcher using WeasyPrint with college comparisons"""
    try:
        # Calculate summary stats
        if not pitch_data:
            print(f"No pitch data for {pitcher_name}")
            return None
            
        # Format pitcher name (convert "Smith, Jack" to "Jack Smith")
        if ', ' in pitcher_name:
            last_name, first_name = pitcher_name.split(', ', 1)
            formatted_name = f"{first_name} {last_name}"
        else:
            formatted_name = pitcher_name
            
        # Determine pitcher handedness from the data
        pitcher_throws = 'Right'  # Default to right-handed
        for pitch in pitch_data:
            if pitch.get('PitcherThrows'):
                pitcher_throws = pitch.get('PitcherThrows')
                break
        
        print(f"Pitcher {formatted_name} throws: {pitcher_throws}")
            
        # Group pitches by type and calculate averages
        pitch_type_data = {}
        
        for pitch in pitch_data:
            pitch_type = pitch.get('TaggedPitchType', 'Unknown')
            if pitch_type not in pitch_type_data:
                pitch_type_data[pitch_type] = {
                    'pitches': [],
                    'count': 0
                }
            pitch_type_data[pitch_type]['pitches'].append(pitch)
            pitch_type_data[pitch_type]['count'] += 1
        
        # Calculate averages for each pitch type WITH college comparisons
        pitch_type_breakdown = []
        
        # Define priority order - Fastball/Sinker first, then alphabetical
        priority_types = ['Fastball', 'Sinker', 'Four-Seam', '4-Seam', 'TwoSeam', 'Two-Seam']
        
        # Sort pitch types with priority
        sorted_pitch_types = []
        
        # Add priority types first (if they exist)
        for priority_type in priority_types:
            for actual_type in pitch_type_data.keys():
                if priority_type.lower() in actual_type.lower() and actual_type not in sorted_pitch_types:
                    sorted_pitch_types.append(actual_type)
                    break
        
        # Add remaining types alphabetically
        remaining_types = [pt for pt in pitch_type_data.keys() if pt not in sorted_pitch_types]
        sorted_pitch_types.extend(sorted(remaining_types))
        
        for pitch_type in sorted_pitch_types:
            pitches = pitch_type_data[pitch_type]['pitches']
            count = pitch_type_data[pitch_type]['count']
            
            # Calculate averages for this pitch type
            velocities = [p.get('RelSpeed', 0) for p in pitches if p.get('RelSpeed')]
            spin_rates = [p.get('SpinRate', 0) for p in pitches if p.get('SpinRate')]
            ivbs = [p.get('InducedVertBreak', 0) for p in pitches if p.get('InducedVertBreak')]
            hbs = [p.get('HorzBreak', 0) for p in pitches if p.get('HorzBreak')]
            rel_sides = [p.get('RelSide', 0) for p in pitches if p.get('RelSide')]
            rel_heights = [p.get('RelHeight', 0) for p in pitches if p.get('RelHeight')]
            extensions = [p.get('Extension', 0) for p in pitches if p.get('Extension')]
            
            # Calculate pitcher's averages
            pitcher_avg_velocity = sum(velocities)/len(velocities) if velocities else None
            pitcher_avg_spin = sum(spin_rates)/len(spin_rates) if spin_rates else None
            pitcher_avg_ivb = sum(ivbs)/len(ivbs) if ivbs else None
            pitcher_avg_hb = sum(hbs)/len(hbs) if hbs else None
            pitcher_avg_rel_side = sum(rel_sides)/len(rel_sides) if rel_sides else None
            pitcher_avg_rel_height = sum(rel_heights)/len(rel_heights) if rel_heights else None
            pitcher_avg_extension = sum(extensions)/len(extensions) if extensions else None
            
            # Get college averages for comparison (with pitcher handedness)
            college_averages = get_college_averages(pitch_type, comparison_level, pitcher_throws)
            
            # Calculate comparisons
            velocity_comp = calculate_percentile(pitcher_avg_velocity, 
                                   college_averages['avg_velocity'] if college_averages else None,
                                   metric_name='velocity',
                                   pitch_type=pitch_type)
            spin_comp = calculate_percentile(pitcher_avg_spin, 
                                           college_averages['avg_spin_rate'] if college_averages else None)
            ivb_comp = calculate_percentile(pitcher_avg_ivb, 
                              college_averages['avg_ivb'] if college_averages else None,
                              metric_name='ivb',
                              pitch_type=pitch_type)
            hb_comp = calculate_percentile(pitcher_avg_hb, 
                             college_averages['avg_hb'] if college_averages else None,
                             metric_name='hb',
                             pitch_type=pitch_type,
                             pitcher_throws=pitcher_throws)
            rel_side_comp = calculate_percentile(pitcher_avg_rel_side, 
                                               college_averages['avg_rel_side'] if college_averages else None)
            rel_height_comp = calculate_percentile(pitcher_avg_rel_height, 
                                                 college_averages['avg_rel_height'] if college_averages else None)
            extension_comp = calculate_percentile(pitcher_avg_extension, 
                                                college_averages['avg_extension'] if college_averages else None)
            
            pitch_type_breakdown.append({
                'name': pitch_type,
                'count': count,
                'avg_velocity': f"{pitcher_avg_velocity:.1f}" if pitcher_avg_velocity else 'N/A',
                'avg_spin': f"{pitcher_avg_spin:.0f}" if pitcher_avg_spin else 'N/A',
                'avg_ivb': f"{pitcher_avg_ivb:.1f}" if pitcher_avg_ivb else 'N/A',
                'avg_hb': f"{pitcher_avg_hb:.1f}" if pitcher_avg_hb else 'N/A',
                'avg_rel_side': f"{pitcher_avg_rel_side:.1f}" if pitcher_avg_rel_side else 'N/A',
                'avg_rel_height': f"{pitcher_avg_rel_height:.1f}" if pitcher_avg_rel_height else 'N/A',
                'avg_extension': f"{pitcher_avg_extension:.1f}" if pitcher_avg_extension else 'N/A',
                # College comparison data - always include, even if N/A
                'college_velocity': f"{college_averages['avg_velocity']:.1f}" if college_averages and college_averages['avg_velocity'] else 'N/A',
                'college_spin': f"{college_averages['avg_spin_rate']:.0f}" if college_averages and college_averages['avg_spin_rate'] else 'N/A',
                'college_ivb': f"{college_averages['avg_ivb']:.1f}" if college_averages and college_averages['avg_ivb'] else 'N/A',
                'college_hb': f"{college_averages['avg_hb']:.1f}" if college_averages and college_averages['avg_hb'] else 'N/A',
                'college_rel_side': f"{college_averages['avg_rel_side']:.1f}" if college_averages and college_averages['avg_rel_side'] else 'N/A',
                'college_rel_height': f"{college_averages['avg_rel_height']:.1f}" if college_averages and college_averages['avg_rel_height'] else 'N/A',
                'college_extension': f"{college_averages['avg_extension']:.1f}" if college_averages and college_averages['avg_extension'] else 'N/A',
                # Comparison indicators - always include, even if None
                'velocity_comp': velocity_comp,
                'spin_comp': spin_comp,
                'ivb_comp': ivb_comp,
                'hb_comp': hb_comp,
                'rel_side_comp': rel_side_comp,
                'rel_height_comp': rel_height_comp,
                'extension_comp': extension_comp,
                'has_college_data': college_averages is not None
            })
        
        summary_stats = {
            'pitch_type_breakdown': pitch_type_breakdown,
            'comparison_level': comparison_level,
            'pitcher_throws': pitcher_throws
        }

        multi_level_stats = get_multi_level_comparisons(pitch_data, pitcher_throws)
        
        print(f"Generating PDF for {formatted_name} ({pitcher_throws}) with {len(pitch_data)} pitches and {comparison_level} comparisons")
        
        # Read HTML template
        try:
            with open('pitcher_report.html', 'r', encoding='utf-8') as file:
                html_template = file.read()
        except FileNotFoundError:
            print("Error: pitcher_report.html not found. Make sure it's in the same directory as app.py")
            return None
        
        # Render template with data using Jinja2
        template = Template(html_template)
        rendered_html = template.render(
            pitcher_name=formatted_name,
            date=date,
            summary_stats=summary_stats,
            pitch_data=pitch_data,
            multi_level_stats=multi_level_stats  # Add this line
        )
        
        # Generate PDF using WeasyPrint with proper base_url for static files
        try:
            # Get the absolute path to the current directory so WeasyPrint can find static files
            base_url = f"file://{os.path.abspath('.')}/"
            print(f"Using base_url: {base_url}")
            
            # Check if static/pbr.png exists
            static_image_path = os.path.join(os.getcwd(), 'static', 'pbr.png')
            if os.path.exists(static_image_path):
                print(f"Found image at: {static_image_path}")
            else:
                print(f"Warning: Image not found at {static_image_path}")
            
            html_doc = weasyprint.HTML(string=rendered_html, base_url=base_url)
            pdf_bytes = html_doc.write_pdf()
            print(f"PDF generated successfully for {formatted_name}")
            return pdf_bytes
        except Exception as e:
            print(f"WeasyPrint error: {str(e)}")
            return None
        
    except Exception as e:
        print(f"Error generating PDF for {pitcher_name}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def send_pitcher_email(pitcher_name, email, pitch_data, date, comparison_level='D1'):
    """Send email to pitcher with PDF attachment (using WeasyPrint)"""
    try:
        # Check if email config is available
        if not EMAIL_USERNAME or not EMAIL_PASSWORD:
            print("Email configuration not available. Please check email_config.json")
            return False
        
        # Generate PDF using WeasyPrint with college comparisons
        pdf_data = generate_pitcher_pdf(pitcher_name, pitch_data, date, comparison_level)
        if not pdf_data:
            print(f"Failed to generate PDF for {pitcher_name}")
            return False
        
        # Format pitcher name for display
        if ', ' in pitcher_name:
            last_name, first_name = pitcher_name.split(', ', 1)
            display_name = f"{first_name} {last_name}"
        else:
            display_name = pitcher_name
        
        # Calculate basic stats for email body
        total_pitches = len(pitch_data) if pitch_data else 0
        
        # Create email content
        subject = f"Your Pitching Performance Report with {comparison_level} Comparisons - {date}"
        
        body = f"""Hi {display_name},

Your pitching performance report for {date} is attached as a PDF.

Report Summary:
- Total Pitches: {total_pitches}
- Includes comparison to {comparison_level} college baseball averages
- Detailed analysis and stats are in the attached PDF report

Keep up the great work!

Best regards,
Coaching Staff
"""
        
        # Create email message
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = email
        
        # Add body
        msg.attach(MIMEText(body, 'plain'))
        
        # Add PDF attachment
        pdf_attachment = MIMEBase('application', 'octet-stream')
        pdf_attachment.set_payload(pdf_data)
        encoders.encode_base64(pdf_attachment)
        
        # Create filename (use display name for filename)
        safe_name = display_name.replace(" ", "_").replace(",", "")
        filename = f"{safe_name}_Report_{date}.pdf"
        
        pdf_attachment.add_header(
            'Content-Disposition',
            f'attachment; filename="{filename}"'
        )
        msg.attach(pdf_attachment)
        
        # Send email
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        print(f"Email with PDF sent successfully to {display_name} at {email}")
        return True
        
    except Exception as e:
        print(f"Failed to send email to {pitcher_name} at {email}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

@app.route('/api/send-emails', methods=['POST'])
def send_emails():
    """API endpoint to send emails to pitchers with their data"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    try:
        data = request.get_json()
        selected_date = data.get('date')
        comparison_level = data.get('comparison_level', 'D1')  # Default to D1
        
        if not selected_date:
            return jsonify({'error': 'Date is required'}), 400
        
        # Validate comparison level
        valid_levels = ['D1', 'D2', 'D3', 'SEC']
        if comparison_level not in valid_levels:
            return jsonify({'error': f'Invalid comparison level. Must be one of: {valid_levels}'}), 400
        
        # Get pitchers for the selected date
        pitchers_query = """
        SELECT DISTINCT Pitcher
        FROM `V1PBR.Test`
        WHERE CAST(Date AS STRING) = @date
        AND Pitcher IS NOT NULL
        ORDER BY Pitcher
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "STRING", selected_date),
            ]
        )
        
        pitchers_result = client.query(pitchers_query, job_config=job_config)
        pitchers_from_test = [row.Pitcher for row in pitchers_result]
        
        # Get ALL prospect info from Info table
        prospects_query = """
        SELECT Event, Prospect, Email, Type
        FROM `V1PBRInfo.Info`
        ORDER BY Prospect
        """
        
        prospects_result = client.query(prospects_query)
        all_prospects = []
        prospects_dict = {}
        
        for row in prospects_result:
            prospect_info = {
                'name': row.Prospect,
                'email': row.Email,
                'type': row.Type,
                'event': row.Event
            }
            all_prospects.append(prospect_info)
            if row.Email:  # Only add to dict if email exists
                prospects_dict[row.Prospect] = prospect_info
        
        # Analyze matches and mismatches
        matched_prospects = []
        unmatched_prospects = []
        sent_emails = []
        failed_emails = []
        
        # Check each prospect against pitchers from Test table
        for prospect in all_prospects:
            prospect_name = prospect['name']
            if prospect_name in pitchers_from_test:
                # This prospect has pitch data
                if prospect['email']:
                    # Has email, can send
                    matched_prospects.append(prospect)
                    
                    # Get pitcher's detailed data and try to send email
                    pitcher_data_query = """
                    SELECT *
                    FROM `V1PBR.Test`
                    WHERE CAST(Date AS STRING) = @date
                    AND Pitcher = @pitcher
                    ORDER BY PitchNo
                    """
                    
                    pitcher_job_config = bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("date", "STRING", selected_date),
                            bigquery.ScalarQueryParameter("pitcher", "STRING", prospect_name),
                        ]
                    )
                    
                    pitcher_result = client.query(pitcher_data_query, job_config=pitcher_job_config)
                    pitch_data = [dict(row) for row in pitcher_result]
                    
                    # Try to send email with college comparisons
                    print(f"Attempting to send email to {prospect_name} at {prospect['email']} with {comparison_level} comparisons")
                    email_success = send_pitcher_email(prospect_name, prospect['email'], pitch_data, selected_date, comparison_level)
                    print(f"Email result for {prospect_name}: {email_success}")
                    
                    if email_success:
                        sent_emails.append({
                            'pitcher': prospect_name,
                            'email': prospect['email'],
                            'type': prospect['type'],
                            'event': prospect['event'],
                            'pitch_count': len(pitch_data),
                            'comparison_level': comparison_level
                        })
                    else:
                        failed_emails.append({
                            'pitcher': prospect_name,
                            'email': prospect['email'],
                            'type': prospect['type'],
                            'event': prospect['event'],
                            'error': 'Email sending failed'
                        })
                else:
                    # Has pitch data but no email
                    unmatched_prospects.append({
                        'name': prospect_name,
                        'email': prospect['email'] or 'No email',
                        'type': prospect['type'],
                        'event': prospect['event'],
                        'reason': 'No email address in Info table'
                    })
            else:
                # This prospect doesn't have pitch data for this date
                unmatched_prospects.append({
                    'name': prospect_name,
                    'email': prospect['email'] or 'No email',
                    'type': prospect['type'],
                    'event': prospect['event'],
                    'reason': 'No pitch data for this date'
                })
        
        # Summary statistics
        total_prospects = len(all_prospects)
        total_matched = len(matched_prospects)
        total_sent = len(sent_emails)
        total_failed = len(failed_emails)
        total_unmatched = len(unmatched_prospects)
        
        return jsonify({
            'success': True,
            'summary': {
                'total_prospects_in_info': total_prospects,
                'prospects_with_pitch_data': total_matched,
                'emails_sent_successfully': total_sent,
                'emails_failed': total_failed,
                'prospects_unmatched': total_unmatched,
                'match_rate_percentage': round((total_matched / total_prospects) * 100, 1) if total_prospects > 0 else 0,
                'comparison_level': comparison_level
            },
            'sent_emails': sent_emails,
            'failed_emails': failed_emails,
            'unmatched_prospects': unmatched_prospects,
            'pitchers_in_test_table': pitchers_from_test
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/matched-prospects')
def get_matched_prospects():
    """API endpoint to get prospects from Info table that have pitch data for a specific date"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify({'error': 'Date parameter is required'}), 400
    
    try:
        # Get pitchers from Test table for the selected date
        pitchers_query = """
        SELECT DISTINCT Pitcher
        FROM `V1PBR.Test`
        WHERE CAST(Date AS STRING) = @date
        AND Pitcher IS NOT NULL
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "STRING", selected_date),
            ]
        )
        
        pitchers_result = client.query(pitchers_query, job_config=job_config)
        pitchers_from_test = set([row.Pitcher for row in pitchers_result])
        
        # Get prospects from Info table that match pitchers in Test table
        prospects_query = """
        SELECT Event, Prospect, Email, Type
        FROM `V1PBRInfo.Info`
        WHERE Email IS NOT NULL
        ORDER BY Prospect
        """
        
        prospects_result = client.query(prospects_query)
        matched_prospects = []
        
        for row in prospects_result:
            if row.Prospect in pitchers_from_test:
                matched_prospects.append({
                    'name': row.Prospect,
                    'email': row.Email,
                    'type': row.Type,
                    'event': row.Event
                })
        
        return jsonify({'prospects': matched_prospects})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/send-individual-email', methods=['POST'])
def send_individual_email():
    """API endpoint to send email to a specific pitcher"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    try:
        data = request.get_json()
        selected_date = data.get('date')
        pitcher_name = data.get('pitcher_name')
        pitcher_email = data.get('pitcher_email')
        comparison_level = data.get('comparison_level', 'D1')
        
        if not selected_date or not pitcher_name or not pitcher_email:
            return jsonify({'error': 'Date, pitcher name, and email are required'}), 400
        
        # Validate comparison level
        valid_levels = ['D1', 'D2', 'D3', 'SEC']
        if comparison_level not in valid_levels:
            return jsonify({'error': f'Invalid comparison level. Must be one of: {valid_levels}'}), 400
        
        # Get pitcher's detailed data
        pitcher_data_query = """
        SELECT *
        FROM `V1PBR.Test`
        WHERE CAST(Date AS STRING) = @date
        AND Pitcher = @pitcher
        ORDER BY PitchNo
        """
        
        pitcher_job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("date", "STRING", selected_date),
                bigquery.ScalarQueryParameter("pitcher", "STRING", pitcher_name),
            ]
        )
        
        pitcher_result = client.query(pitcher_data_query, job_config=pitcher_job_config)
        pitch_data = [dict(row) for row in pitcher_result]
        
        if not pitch_data:
            return jsonify({'error': f'No pitch data found for {pitcher_name} on {selected_date}'}), 400
        
        # Send email
        print(f"Attempting to send individual email to {pitcher_name} at {pitcher_email} with {comparison_level} comparisons")
        email_success = send_pitcher_email(pitcher_name, pitcher_email, pitch_data, selected_date, comparison_level)
        
        if email_success:
            return jsonify({
                'success': True,
                'message': f'Email sent successfully to {pitcher_name} at {pitcher_email}',
                'pitcher_name': pitcher_name,
                'email': pitcher_email,
                'pitch_count': len(pitch_data),
                'comparison_level': comparison_level,
                'date': selected_date
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to send email to {pitcher_name} at {pitcher_email}'
            })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    import os
    if not os.path.exists('templates'):
        os.makedirs('templates')
        print("Created templates directory")
    
    print("Starting Flask server...")
    print("Make sure harvard-baseball-13fab221b2d4.json is in the same directory")
    print("Make sure templates/index.html exists")
    print("Make sure pitcher_report.html exists")
    app.run(debug=True, host='0.0.0.0', port=5000)
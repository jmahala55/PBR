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


@app.route('/api/pitcher-summary')
def get_pitcher_summary():
    """API endpoint to get pitcher summary with automatic comparison level"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    selected_date = request.args.get('date')
    pitcher_name = request.args.get('pitcher')
    
    if not selected_date or not pitcher_name:
        return jsonify({'error': 'Date and pitcher parameters are required'}), 400
    
    try:
        # Get pitcher's detailed data
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
        pitch_data = [dict(row) for row in result]
        
        if not pitch_data:
            return jsonify({'error': 'No pitch data found'}), 404
        
        # Get comparison level from Info table
        comparison_level = get_pitcher_competition_level(pitcher_name)
        
        # Determine pitcher handedness
        pitcher_throws = 'Right'
        for pitch in pitch_data:
            if pitch.get('PitcherThrows'):
                pitcher_throws = pitch.get('PitcherThrows')
                break
        
        # Generate multi-level comparisons using the pitcher's competition level
        multi_level_stats = get_multi_level_comparisons(pitch_data, pitcher_throws)
        
        # Generate movement plot
        movement_plot_svg = generate_movement_plot_svg(pitch_data)
        pitch_location_plot_svg = generate_pitch_location_plot_svg(pitch_data)
        
        # Calculate zone rates
        zone_rate_data = calculate_zone_rates(pitch_data)
        
        return jsonify({
            'pitch_data': pitch_data,
            'multi_level_stats': multi_level_stats,
            'movement_plot_svg': movement_plot_svg,
            'pitch_location_plot_svg': pitch_location_plot_svg,
            'zone_rate_data': zone_rate_data,  # Add this new data
            'comparison_level': comparison_level,
            'pitcher_throws': pitcher_throws
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/matched-prospects')
def get_matched_prospects():
    """API endpoint to get prospects that have both pitch data and email info"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify({'error': 'Date parameter is required'}), 400
    
    try:
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
        
        # Get prospect info from Info table
        prospects_query = """
        SELECT Event, Prospect, Email, Type, Comp
        FROM `V1PBRInfo.Info`
        WHERE Prospect IS NOT NULL
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
                    'event': row.Event,
                    'comp': row.Comp or 'D1'
                })
        
        return jsonify({'prospects': matched_prospects})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def calculate_zone_rates(pitch_data):
    """Calculate zone rates for each pitch type and overall"""
    try:
        # Strike zone boundaries (in feet, same as used in the plot)
        strike_zone = {
            'xmin': -9.97/12,  # Convert inches to feet
            'xmax': 9.97/12,
            'ymin': 18.00/12,
            'ymax': 40.53/12
        }
        
        def is_in_zone(plate_side, plate_height):
            """Check if a pitch is in the strike zone"""
            if plate_side is None or plate_height is None:
                return False
            
            # Flip plate_side for batter's perspective (same as in the plot)
            flipped_plate_side = -1 * float(plate_side)
            plate_height_float = float(plate_height)
            
            return (strike_zone['xmin'] <= flipped_plate_side <= strike_zone['xmax'] and
                    strike_zone['ymin'] <= plate_height_float <= strike_zone['ymax'])
        
        # Group pitches by type and calculate zone rates
        pitch_type_data = {}
        total_pitches = 0
        total_in_zone = 0
        
        for pitch in pitch_data:
            pitch_type = pitch.get('TaggedPitchType', 'Unknown')
            plate_side = pitch.get('PlateLocSide')
            plate_height = pitch.get('PlateLocHeight')
            
            # Skip pitches without location data
            if plate_side is None or plate_height is None:
                continue
                
            total_pitches += 1
            in_zone = is_in_zone(plate_side, plate_height)
            
            if in_zone:
                total_in_zone += 1
            
            if pitch_type not in pitch_type_data:
                pitch_type_data[pitch_type] = {
                    'total': 0,
                    'in_zone': 0
                }
            
            pitch_type_data[pitch_type]['total'] += 1
            if in_zone:
                pitch_type_data[pitch_type]['in_zone'] += 1
        
        # Calculate zone rate percentages for each pitch type
        zone_rates = {}
        for pitch_type, data in pitch_type_data.items():
            if data['total'] > 0:
                zone_rate = (data['in_zone'] / data['total']) * 100
                zone_rates[pitch_type] = {
                    'zone_rate': zone_rate,
                    'in_zone': data['in_zone'],
                    'total': data['total']
                }
        
        # Calculate overall zone rate
        overall_zone_rate = (total_in_zone / total_pitches * 100) if total_pitches > 0 else 0
        
        return {
            'pitch_type_zone_rates': zone_rates,
            'overall_zone_rate': overall_zone_rate,
            'total_pitches_with_location': total_pitches,
            'total_in_zone': total_in_zone
        }
        
    except Exception as e:
        print(f"Error calculating zone rates: {str(e)}")
        return None

def generate_pitch_location_plot_svg(pitch_data, width=700, height=600):

    try:
        # Group pitches by type
        pitch_types = {}
        for pitch in pitch_data:
            pitch_type = pitch.get('TaggedPitchType')
            if pitch_type and pitch.get('PlateLocSide') is not None and pitch.get('PlateLocHeight') is not None:
                if pitch_type not in pitch_types:
                    pitch_types[pitch_type] = []
                pitch_types[pitch_type].append({
                    'plate_side': -1 * float(pitch.get('PlateLocSide', 0)),  # Flip for batter's perspective
                    'plate_height': float(pitch.get('PlateLocHeight', 0))
                })
        
        if not pitch_types:
            return None
        
        # Define colors for pitch types (same as movement plot)
        colors = {
            'ChangeUp': '#059669', 'Curveball': '#1D4ED8', 'Cutter': '#BE185D',
            'Fastball': '#DC2626', 'Knuckleball': '#9333EA', 'Sinker': '#EA580C',
            'Slider': '#7C3AED', 'Splitter': '#0891B2', 'Sweeper': '#F59E0B',
            'Four-Seam': '#DC2626', '4-Seam': '#DC2626', 'Two-Seam': '#EA580C',
            'TwoSeam': '#EA580C', 'Changeup': '#059669', 'Change-up': '#059669',
            'Curve': '#1D4ED8', 'Cut Fastball': '#BE185D', 'Split-Finger': '#0891B2'
        }
        
        # Set up plot dimensions - wider with space for 3D effect
        margin_left = 60
        margin_right = 150  # Space for legend and 3D effect
        margin_top = 60
        margin_bottom = 60
        plot_width = width - margin_left - margin_right
        plot_height = height - margin_top - margin_bottom
        
        # Define coordinate ranges (feet)
        x_min, x_max = -4, 4
        y_min, y_max = -2, 6
        
        # Scale functions
        def scale_x(x):
            return margin_left + (x - x_min) / (x_max - x_min) * plot_width
        
        def scale_y(y):
            return margin_top + plot_height - (y - y_min) / (y_max - y_min) * plot_height
        
        # Strike zone dimensions (feet)
        strike_zone = {
            'xmin': -9.97/12,  # Convert inches to feet
            'xmax': 9.97/12,
            'ymin': 18.00/12,
            'ymax': 40.53/12
        }
        
        # Larger strike zone (shadow zone)
        larger_strike_zone = {
            'xmin': strike_zone['xmin'] - 2.00/12,
            'xmax': strike_zone['xmax'] + 2.00/12,
            'ymin': strike_zone['ymin'] - 1.47/12,
            'ymax': strike_zone['ymax'] + 1.47/12
        }
        
        # Home plate coordinates - create 3D effect with multiple layers (from old Python code)
        # Base layer (bottom)
        home_plate_base = [
            (-0.7, -0.1),
            (0.7, -0.1),
            (0.7, 0.2),
            (0, 0.5),
            (-0.7, 0.2),
            (-0.7, -0.1)
        ]
        
        # Top layer (slightly offset for 3D effect)
        home_plate_lifted = [
            (-0.7, 0.0),
            (0.7, 0.0),
            (0.7, 0.3),
            (0, 0.6),
            (-0.7, 0.3),
            (-0.7, 0.0)
        ]
        
        # Start SVG
        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
            '<defs>',
            '<style>',
            '.strike-zone { stroke: black; stroke-width: 2; fill: none; }',
            '.shadow-zone { stroke: black; stroke-width: 1; fill: none; stroke-dasharray: 3,3; }',
            '.home-plate-base { stroke: black; stroke-width: 1; fill: #f0f0f0; }',
            '.home-plate-top { stroke: black; stroke-width: 2; fill: white; }',
            '.plate-connector { stroke: black; stroke-width: 1; }',
            '.batter-box { stroke: black; stroke-width: 3; fill: none; }',
            '.axis-text { font-family: Arial, sans-serif; font-size: 10px; fill: black; }',
            '.plot-title { font-family: Arial, sans-serif; font-size: 16px; font-weight: bold; fill: #1a1a1a; text-anchor: start; }',
            '.plot-subtitle { font-family: Arial, sans-serif; font-size: 12px; fill: #666666; text-anchor: start; font-style: italic; }',
            '.legend-text { font-family: Arial, sans-serif; font-size: 9px; fill: black; }',
            '</style>',
            '</defs>',
            
            # White background
            f'<rect width="{width}" height="{height}" fill="white"/>',
        ]
        
        # Title and subtitle
        svg_parts.extend([
            f'<text x="{margin_left}" y="25" class="plot-title">Pitch Location</text>',
            f'<text x="{margin_left}" y="40" class="plot-subtitle">Batter\'s Perspective</text>'
        ])
        
        # Draw larger strike zone (shadow zone)
        larger_left = scale_x(larger_strike_zone['xmin'])
        larger_right = scale_x(larger_strike_zone['xmax'])
        larger_bottom = scale_y(larger_strike_zone['ymin'])
        larger_top = scale_y(larger_strike_zone['ymax'])
        
        svg_parts.append(f'<rect x="{larger_left}" y="{larger_top}" width="{larger_right - larger_left}" height="{larger_bottom - larger_top}" class="shadow-zone"/>')
        
        # Draw main strike zone
        zone_left = scale_x(strike_zone['xmin'])
        zone_right = scale_x(strike_zone['xmax'])
        zone_bottom = scale_y(strike_zone['ymin'])
        zone_top = scale_y(strike_zone['ymax'])
        
        svg_parts.append(f'<rect x="{zone_left}" y="{zone_top}" width="{zone_right - zone_left}" height="{zone_bottom - zone_top}" class="strike-zone"/>')
        
        # Draw strike zone grid lines (thirds)
        # Horizontal lines
        third_height = (strike_zone['ymax'] - strike_zone['ymin']) / 3
        for i in [1, 2]:
            y_pos = scale_y(strike_zone['ymin'] + i * third_height)
            svg_parts.append(f'<line x1="{zone_left}" y1="{y_pos}" x2="{zone_right}" y2="{y_pos}" stroke="black" stroke-width="1"/>')
        
        # Vertical lines
        third_width = (strike_zone['xmax'] - strike_zone['xmin']) / 3
        for i in [1, 2]:
            x_pos = scale_x(strike_zone['xmin'] + i * third_width)
            svg_parts.append(f'<line x1="{x_pos}" y1="{zone_top}" x2="{x_pos}" y2="{zone_bottom}" stroke="black" stroke-width="1"/>')
        
        # Draw home plate with 3D effect (like R code)
        # Draw base plate (bottom layer)
        base_points = []
        for x, y in home_plate_base:
            base_points.append(f"{scale_x(x)},{scale_y(y)}")
        svg_parts.append(f'<polygon points="{" ".join(base_points)}" class="home-plate-base"/>')
        
        # Draw lifted plate (top layer)
        lifted_points = []
        for x, y in home_plate_lifted:
            lifted_points.append(f"{scale_x(x)},{scale_y(y)}")
        svg_parts.append(f'<polygon points="{" ".join(lifted_points)}" class="home-plate-top"/>')
        
        # Draw connecting lines for 3D effect (like the R code segments)
        for i in range(len(home_plate_base)):
            x1, y1 = home_plate_base[i]
            x2, y2 = home_plate_lifted[i]
            svg_parts.append(f'<line x1="{scale_x(x1)}" y1="{scale_y(y1)}" x2="{scale_x(x2)}" y2="{scale_y(y2)}" class="plate-connector"/>')
        
        # Draw batter's boxes with 3D effect (like R code)
        # Right batter's box
        right_box_back = 1.1
        right_box_front = 0.85
        right_box_outside = 2.5
        box_top = 0.3
        box_bottom = -1.0
        
        # Right box 3D lines (matching R code segments exactly)
        svg_parts.extend([
            # Right box angled front line (matches R: x = 1.1, y = -1, xend = .92, yend = 0.3)
            f'<line x1="{scale_x(1.1)}" y1="{scale_y(-1)}" x2="{scale_x(0.92)}" y2="{scale_y(0.3)}" stroke="black" stroke-width="5"/>',
            # Right box horizontal top line (matches R: x = .85, y = 0.3, xend = 2.5, yend = 0.3)
            f'<line x1="{scale_x(0.85)}" y1="{scale_y(0.3)}" x2="{scale_x(2.5)}" y2="{scale_y(0.3)}" stroke="black" stroke-width="5"/>',
        ])
        
        # Left batter's box (mirror of right)
        svg_parts.extend([
            # Left box angled front line (matches R: x = -1.1, y = -1, xend = -0.92, yend = 0.3)
            f'<line x1="{scale_x(-1.1)}" y1="{scale_y(-1)}" x2="{scale_x(-0.92)}" y2="{scale_y(0.3)}" stroke="black" stroke-width="5"/>',
            # Left box horizontal top line (matches R: x = -0.85, y = 0.3, xend = -2.5, yend = 0.3)
            f'<line x1="{scale_x(-0.85)}" y1="{scale_y(0.3)}" x2="{scale_x(-2.5)}" y2="{scale_y(0.3)}" stroke="black" stroke-width="5"/>',
        ])
        
        # Plot pitch locations
        for pitch_type, pitches in pitch_types.items():
            color = colors.get(pitch_type, '#666666')
            
            for pitch in pitches:
                x_pos = scale_x(pitch['plate_side'])
                y_pos = scale_y(pitch['plate_height'])
                
                # Simple circles for all pitches
                svg_parts.append(f'<circle cx="{x_pos}" cy="{y_pos}" r="4" fill="{color}" fill-opacity="0.7" stroke="white" stroke-width="1"/>')
        
        # Legend (positioned more to the left)
        legend_x = margin_left + plot_width - 100  # Moved 80px to the left
        legend_y = margin_top + 50
        
        # Pitch type legend
        svg_parts.append(f'<text x="{legend_x}" y="{legend_y}" class="legend-text" style="font-weight: bold;">Pitch Types:</text>')
        current_y = legend_y + 15
        
        for pitch_type in pitch_types.keys():
            color = colors.get(pitch_type, '#666666')
            svg_parts.extend([
                f'<circle cx="{legend_x + 5}" cy="{current_y}" r="3" fill="{color}"/>',
                f'<text x="{legend_x + 15}" y="{current_y + 3}" class="legend-text">{pitch_type}</text>'
            ])
            current_y += 15
        
        # Add axis labels
        # X-axis label
        x_center = margin_left + plot_width/2
        svg_parts.append(f'<text x="{x_center}" y="{height - 20}" class="axis-text" text-anchor="middle" style="font-weight: bold;">Plate Location - Side (ft)</text>')
        
        # Y-axis label
        y_center = margin_top + plot_height/2
        svg_parts.append(f'<text x="20" y="{y_center}" class="axis-text" text-anchor="middle" style="font-weight: bold;" transform="rotate(-90, 20, {y_center})">Plate Location - Height (ft)</text>')
        
        # Add tick marks and labels
        # X-axis ticks
        for x in [-3, -2, -1, 0, 1, 2, 3]:
            x_pos = scale_x(x)
            svg_parts.append(f'<line x1="{x_pos}" y1="{margin_top + plot_height}" x2="{x_pos}" y2="{margin_top + plot_height + 5}" stroke="black" stroke-width="1"/>')
            svg_parts.append(f'<text x="{x_pos}" y="{margin_top + plot_height + 18}" class="axis-text" text-anchor="middle">{x}</text>')
        
        # Y-axis ticks
        for y in [0, 1, 2, 3, 4, 5]:
            y_pos = scale_y(y)
            svg_parts.append(f'<line x1="{margin_left - 5}" y1="{y_pos}" x2="{margin_left}" y2="{y_pos}" stroke="black" stroke-width="1"/>')
            svg_parts.append(f'<text x="{margin_left - 10}" y="{y_pos + 3}" class="axis-text" text-anchor="end">{y}</text>')
        
        # Close SVG
        svg_parts.append('</svg>')
        
        return '\n'.join(svg_parts)
        
    except Exception as e:
        print(f"Error generating pitch location plot SVG: {str(e)}")
        return None

# Replace your generate_movement_plot_svg function with this fixed version:
def generate_movement_plot_svg(pitch_data, width=1000, height=500):
    """Generate SVG for both movement plot (left) and release plot (right)"""
    try:
        # Group pitches by type
        pitch_types = {}
        for pitch in pitch_data:
            pitch_type = pitch.get('TaggedPitchType')
            if pitch_type and pitch.get('HorzBreak') is not None and pitch.get('InducedVertBreak') is not None:
                if pitch_type not in pitch_types:
                    pitch_types[pitch_type] = []
                pitch_types[pitch_type].append({
                    'hb': float(pitch.get('HorzBreak', 0)),
                    'ivb': float(pitch.get('InducedVertBreak', 0)),
                    'rel_side': float(pitch.get('RelSide', 0)) if pitch.get('RelSide') is not None else None,
                    'rel_height': float(pitch.get('RelHeight', 0)) if pitch.get('RelHeight') is not None else None
                })
        
        if not pitch_types:
            return None
        
        # Define colors for pitch types
        colors = {
            'ChangeUp': '#059669', 'Curveball': '#1D4ED8', 'Cutter': '#BE185D',
            'Fastball': '#DC2626', 'Knuckleball': '#9333EA', 'Sinker': '#EA580C',
            'Slider': '#7C3AED', 'Splitter': '#0891B2', 'Sweeper': '#F59E0B',
            'Four-Seam': '#DC2626', '4-Seam': '#DC2626', 'Two-Seam': '#EA580C',
            'TwoSeam': '#EA580C', 'Changeup': '#059669', 'Change-up': '#059669',
            'Curve': '#1D4ED8', 'Cut Fastball': '#BE185D', 'Split-Finger': '#0891B2'
        }
        
        # Function to calculate 95% confidence ellipse (only for movement plot)
        def calculate_confidence_ellipse(x_values, y_values, confidence=0.95):
            if len(x_values) < 3:
                return []
            
            import math
            
            x_mean = sum(x_values) / len(x_values)
            y_mean = sum(y_values) / len(y_values)
            
            x_diff = [x - x_mean for x in x_values]
            y_diff = [y - y_mean for y in y_values]
            n = len(x_values)
            
            cov_xx = sum(x * x for x in x_diff) / (n - 1)
            cov_xy = sum(x * y for x, y in zip(x_diff, y_diff)) / (n - 1)
            cov_yy = sum(y * y for y in y_diff) / (n - 1)
            
            trace = cov_xx + cov_yy
            det = cov_xx * cov_yy - cov_xy * cov_xy
            
            if det <= 0:
                return []
            
            lambda1 = (trace + math.sqrt(trace * trace - 4 * det)) / 2
            lambda2 = (trace - math.sqrt(trace * trace - 4 * det)) / 2
            
            if lambda1 <= 0 or lambda2 <= 0:
                return []
            
            scale = math.sqrt(5.991)  # 95% confidence for 2D
            a = scale * math.sqrt(lambda1)
            b = scale * math.sqrt(lambda2)
            
            if abs(cov_xy) < 1e-10:
                theta = 0 if cov_xx >= cov_yy else math.pi / 2
            else:
                theta = math.atan2(2 * cov_xy, cov_xx - cov_yy) / 2
            
            points = []
            for t in [i * 0.1 for i in range(int(2 * math.pi / 0.1) + 1)]:
                x = a * math.cos(t) * math.cos(theta) - b * math.sin(t) * math.sin(theta) + x_mean
                y = a * math.cos(t) * math.sin(theta) + b * math.sin(t) * math.cos(theta) + y_mean
                points.append((x, y))
            
            return points
        
        # Set up plot dimensions - two side-by-side plots
        margin_left = 60  # More space for left axis labels
        margin_right = 40  # Less space on right
        margin_top = 40
        margin_bottom = 40
        center_gap = 40  # Gap between plots
        
        plot_width = (width - margin_left - margin_right - center_gap) // 2
        plot_height = height - margin_top - margin_bottom
        
        # Movement plot (left)
        mov_x_start = margin_left
        mov_y_start = margin_top
        
        # Release plot (right)
        rel_x_start = margin_left + plot_width + center_gap
        rel_y_start = margin_top
        
        # Scale functions for movement plot
        mov_x_min, mov_x_max = -30, 30
        mov_y_min, mov_y_max = -30, 30
        
        def scale_mov_x(x):
            return mov_x_start + (x - mov_x_min) / (mov_x_max - mov_x_min) * plot_width
        
        def scale_mov_y(y):
            return mov_y_start + plot_height - (y - mov_y_min) / (mov_y_max - mov_y_min) * plot_height
        
        # Scale functions for release plot
        rel_x_min, rel_x_max = -5, 5
        rel_y_min, rel_y_max = 0, 8
        
        def scale_rel_x(x):
            return rel_x_start + (x - rel_x_min) / (rel_x_max - rel_x_min) * plot_width
        
        def scale_rel_y(y):
            return rel_y_start + plot_height - (y - rel_y_min) / (rel_y_max - rel_y_min) * plot_height
        
        # Calculate positions for axis labels (FIXED - calculate outside f-strings)
        mov_center_x = mov_x_start + plot_width/2
        mov_bottom_y = height - 10
        mov_left_y = mov_y_start + plot_height/2
        
        rel_center_x = rel_x_start + plot_width/2
        rel_right_x = width - 15
        rel_center_y = rel_y_start + plot_height/2
        
        # Start SVG
        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
            '<defs>',
            '<style>',
            '.axis-line { stroke: #990000; stroke-width: 2; }',
            '.grid-line { stroke: rgba(0,0,0,0.2); stroke-width: 1; }',
            '.axis-text { font-family: Arial, sans-serif; font-size: 10px; fill: black; }',
            '.axis-title { font-family: Arial, sans-serif; font-size: 12px; font-weight: bold; fill: black; }',
            '.legend-text { font-family: Arial, sans-serif; font-size: 9px; fill: black; }',
            '.plot-title { font-family: Arial, sans-serif; font-size: 14px; font-weight: bold; fill: #1a1a1a; text-anchor: start; }',
            '.plot-subtitle { font-family: Arial, sans-serif; font-size: 10px; fill: #666666; text-anchor: start; font-style: italic; }',
            '.plot-border { stroke: black; stroke-width: 2; fill: none; }',
            '.confidence-ellipse { fill: none; stroke-width: 1.5; stroke-opacity: 0.7; }',
            '.home-plate { stroke: #990000; stroke-width: 2; fill: white; }',
            '.release-text { font-family: Arial, sans-serif; font-size: 12px; font-weight: bold; fill: #990000; text-anchor: middle; }',
            '</style>',
            '</defs>',
            
            # White background
            f'<rect width="{width}" height="{height}" fill="white"/>',
        ]
        
        # === MOVEMENT PLOT (LEFT) ===
        
        # Movement plot titles (left-aligned)
        svg_parts.append(f'<text x="{mov_x_start}" y="20" class="plot-title">Movement Profile</text>')
        svg_parts.append(f'<text x="{mov_x_start}" y="32" class="plot-subtitle">Pitcher\'s Perspective</text>')
        
        # Movement plot grid
        for x in range(-25, 31, 5):  # Every 5 units like before
            x_pos = scale_mov_x(x)
            line_class = 'axis-line' if x == 0 else 'grid-line'
            svg_parts.append(f'<line x1="{x_pos}" y1="{mov_y_start}" x2="{x_pos}" y2="{mov_y_start + plot_height}" class="{line_class}"/>')
            if x != 0:
                svg_parts.append(f'<text x="{x_pos}" y="{mov_y_start + plot_height + 15}" class="axis-text" text-anchor="middle">{x}</text>')
        
        for y in range(-25, 31, 5):  # Every 5 units like before
            y_pos = scale_mov_y(y)
            line_class = 'axis-line' if y == 0 else 'grid-line'
            svg_parts.append(f'<line x1="{mov_x_start}" y1="{y_pos}" x2="{mov_x_start + plot_width}" y2="{y_pos}" class="{line_class}"/>')
            if y != 0:
                svg_parts.append(f'<text x="{mov_x_start - 10}" y="{y_pos + 3}" class="axis-text" text-anchor="end">{y}</text>')
        
        # Movement plot border
        svg_parts.append(f'<rect x="{mov_x_start}" y="{mov_y_start}" width="{plot_width}" height="{plot_height}" class="plot-border"/>')
        
        # Movement plot axis labels (FIXED - use pre-calculated positions)
        svg_parts.extend([
            f'<text x="{mov_center_x}" y="{mov_bottom_y}" class="axis-title" text-anchor="middle">Horizontal Break (in)</text>',
            f'<text x="20" y="{mov_left_y}" class="axis-title" text-anchor="middle" transform="rotate(-90, 20, {mov_left_y})">Induced Vertical Break (in)</text>'
        ])
        
        # === RELEASE PLOT (RIGHT) ===
        
        # Release plot titles (left-aligned)
        svg_parts.append(f'<text x="{rel_x_start}" y="20" class="plot-title">Release Point</text>')
        svg_parts.append(f'<text x="{rel_x_start}" y="32" class="plot-subtitle">Pitcher\'s Perspective</text>')
        
        # Release plot grid
        for x in range(-4, 5, 1):  # Every 1 foot like before
            x_pos = scale_rel_x(x)
            line_class = 'axis-line' if x == 0 else 'grid-line'
            svg_parts.append(f'<line x1="{x_pos}" y1="{rel_y_start}" x2="{x_pos}" y2="{rel_y_start + plot_height}" class="{line_class}"/>')
            if x != 0:
                svg_parts.append(f'<text x="{x_pos}" y="{rel_y_start + plot_height + 15}" class="axis-text" text-anchor="middle">{x}</text>')
        
        for y in range(1, 8, 1):  # Every 1 foot like before
            y_pos = scale_rel_y(y)
            svg_parts.append(f'<line x1="{rel_x_start}" y1="{y_pos}" x2="{rel_x_start + plot_width}" y2="{y_pos}" class="grid-line"/>')
            svg_parts.append(f'<text x="{rel_x_start - 10}" y="{y_pos + 3}" class="axis-text" text-anchor="end">{y}</text>')
        
        # Release plot border
        svg_parts.append(f'<rect x="{rel_x_start}" y="{rel_y_start}" width="{plot_width}" height="{plot_height}" class="plot-border"/>')
        
        # Release plot axis labels (FIXED - use pre-calculated positions)
        svg_parts.extend([
            f'<text x="{rel_center_x}" y="{mov_bottom_y}" class="axis-title" text-anchor="middle">Release Side (ft)</text>',
            f'<text x="{rel_right_x}" y="{rel_center_y}" class="axis-title" text-anchor="middle" transform="rotate(-90, {rel_right_x}, {rel_center_y})">Release Height (ft)</text>'
        ])
        
        # Add LHP/RHP labels to release plot
        svg_parts.extend([
            f'<text x="{scale_rel_x(-4)}" y="{scale_rel_y(7.5)}" class="release-text">LHP</text>',
            f'<text x="{scale_rel_x(4)}" y="{scale_rel_y(7.5)}" class="release-text">RHP</text>'
        ])
        
        # Add home plate to release plot (wider and shorter)
        plate_left = scale_rel_x(-1.0)  # Extended from -0.7 to -1.0
        plate_right = scale_rel_x(1.0)  # Extended from 0.7 to 1.0
        plate_top = scale_rel_y(1.0)    # Moved up from 1.2 to 1.0 (shorter)
        plate_bottom = scale_rel_y(0.7) # Moved up from 0.5 to 0.7 (shorter)
        svg_parts.append(f'<rect x="{plate_left}" y="{plate_top}" width="{plate_right - plate_left}" height="{plate_bottom - plate_top}" class="home-plate"/>')
        
        # Determine pitcher handedness for average release point
        pitcher_throws = 'Right'  # Default
        for pitch_list in pitch_types.values():
            for pitch in pitch_list:
                # We'd need to get this from the original data, but for now use default
                break
            break
        
        # Add average release point (open circle)
        avg_rel_side = -1.7 if pitcher_throws == 'Left' else 1.66
        avg_rel_height = 5.7
        avg_x = scale_rel_x(avg_rel_side)
        avg_y = scale_rel_y(avg_rel_height)
        svg_parts.append(f'<circle cx="{avg_x}" cy="{avg_y}" r="6" fill="white" stroke="black" stroke-width="2"/>')
        
        # Plot data for both charts
        for pitch_type, pitches in pitch_types.items():
            color = colors.get(pitch_type, '#666666')
            
            # Extract coordinates
            hb_values = [p['hb'] for p in pitches]
            ivb_values = [p['ivb'] for p in pitches]
            rel_side_values = [p['rel_side'] for p in pitches if p['rel_side'] is not None]
            rel_height_values = [p['rel_height'] for p in pitches if p['rel_height'] is not None]
            
            # === MOVEMENT PLOT DATA ===
            
            # Draw 95% confidence ellipse for movement
            if len(pitches) >= 3:
                ellipse_points = calculate_confidence_ellipse(hb_values, ivb_values)
                if ellipse_points:
                    path_data = []
                    for i, (x, y) in enumerate(ellipse_points):
                        x_pos = scale_mov_x(x)
                        y_pos = scale_mov_y(y)
                        if i == 0:
                            path_data.append(f'M {x_pos} {y_pos}')
                        else:
                            path_data.append(f'L {x_pos} {y_pos}')
                    path_data.append('Z')
                    svg_parts.append(f'<path d="{" ".join(path_data)}" class="confidence-ellipse" stroke="{color}"/>')
            
            # Movement individual points
            for pitch in pitches:
                x_pos = scale_mov_x(pitch['hb'])
                y_pos = scale_mov_y(pitch['ivb'])
                svg_parts.append(f'<circle cx="{x_pos}" cy="{y_pos}" r="2.5" fill="{color}" fill-opacity="0.6" stroke="rgba(255,255,255,0.4)" stroke-width="0.5"/>')
            
            # Movement average point
            if pitches:
                avg_hb = sum(p['hb'] for p in pitches) / len(pitches)
                avg_ivb = sum(p['ivb'] for p in pitches) / len(pitches)
                avg_x = scale_mov_x(avg_hb)
                avg_y = scale_mov_y(avg_ivb)
                svg_parts.append(f'<circle cx="{avg_x}" cy="{avg_y}" r="5" fill="{color}" stroke="rgba(0,0,0,0.8)" stroke-width="2"/>')
            
            # === RELEASE PLOT DATA ===
            
            # Release individual points (only if we have release data)
            if rel_side_values and rel_height_values:
                for i, pitch in enumerate(pitches):
                    if pitch['rel_side'] is not None and pitch['rel_height'] is not None:
                        x_pos = scale_rel_x(pitch['rel_side'])
                        y_pos = scale_rel_y(pitch['rel_height'])
                        svg_parts.append(f'<circle cx="{x_pos}" cy="{y_pos}" r="2.5" fill="{color}" fill-opacity="0.6" stroke="rgba(255,255,255,0.4)" stroke-width="0.5"/>')
        
        # Legend (bottom right of movement plot)
        legend_y_start = mov_y_start + plot_height - 20
        legend_x = mov_x_start + plot_width - 120
        current_legend_y = legend_y_start
        
        for pitch_type in pitch_types.keys():
            color = colors.get(pitch_type, '#666666')
            svg_parts.extend([
                f'<circle cx="{legend_x}" cy="{current_legend_y}" r="3" fill="{color}"/>',
                f'<text x="{legend_x + 10}" y="{current_legend_y + 3}" class="legend-text">{pitch_type}</text>'
            ])
            current_legend_y -= 15
        
        # Close SVG
        svg_parts.append('</svg>')
        
        return '\n'.join(svg_parts)
        
    except Exception as e:
        print(f"Error generating movement plot SVG: {str(e)}")
        return None


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

def is_horizontal_break_better(difference, pitch_type, pitcher_throws):
    """Determine if horizontal break difference is better based on pitch type and handedness"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories based on expected break patterns
    # These should break away from arm side (glove side break)
    breaking_balls = ['curveball', 'curve', 'slider', 'cutter', 'cut fastball', 'sweeper']
    
    # These should have arm-side run
    fastballs_and_offspeed = [
        'fastball', 'four-seam', '4-seam', 'fourseam', 'four seam',
        'sinker', 'two-seam', '2-seam', 'twoseam', 'two seam',
        'changeup', 'change-up', 'change up', 'changup',
        'splitter', 'split-finger', 'splitfinger', 'split finger',
        'knuckleball', 'knuckle ball'
    ]
    
    # Check if pitch type matches any variation
    is_breaking_ball = any(pattern in pitch_type_lower for pattern in breaking_balls)
    is_fastball_or_offspeed = any(pattern in pitch_type_lower for pattern in fastballs_and_offspeed)
    
    if pitcher_throws == 'Right':
        if is_breaking_ball:
            # RHP breaking balls should go negative (toward 1B) - more negative is better
            return difference < 0
        elif is_fastball_or_offspeed:
            # RHP fastballs/offspeed should go positive (toward 3B) - more positive is better
            return difference > 0
    elif pitcher_throws == 'Left':
        if is_breaking_ball:
            # LHP breaking balls should go positive (toward 3B) - more positive is better
            return difference > 0
        elif is_fastball_or_offspeed:
            # LHP fastballs/offspeed should go negative (toward 1B) - more negative is better
            return difference < 0
    
    # Default case: if pitch type doesn't match known categories, assume more is better
    return difference > 0


def is_ivb_better(difference, pitch_type):
    """Determine if IVB difference is better based on pitch type"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories where negative IVB is better (breaking balls and offspeed)
    negative_ivb_pitches = [
        'curveball', 'curve', 
        'changeup', 'change-up', 'change up', 'changup',
        'splitter', 'split-finger', 'splitfinger', 'split finger',
        'knuckleball', 'knuckle ball',
        'sinker', 'two-seam', '2-seam', 'twoseam', 'two seam'  # Added sinker variations
    ]
    
    # Check for matches (using 'in' to catch variations)
    is_negative_ivb_pitch = any(pattern in pitch_type_lower for pattern in negative_ivb_pitches)
    
    if is_negative_ivb_pitch:
        # For these pitches, more negative IVB is better (more drop/sink)
        return difference < 0
    else:
        # For fastballs, cutters, sliders, sweepers - more positive IVB is better (more carry/rise)
        return difference > 0

def is_velocity_better(difference, pitch_type):
    """Determine if velocity difference is better based on pitch type"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories where lower velocity is better (offspeed pitches)
    lower_velo_pitches = [
        'changeup', 'change-up', 'change up', 'changup',
        'splitter', 'split-finger', 'splitfinger', 'split finger',
        'knuckleball', 'knuckle ball'
    ]
    
    # Check for matches
    is_lower_velo_pitch = any(pattern in pitch_type_lower for pattern in lower_velo_pitches)
    
    if is_lower_velo_pitch:
        # For changeups, splitters, and knuckleballs, lower velocity is better
        return difference < 0
    else:
        # For all other pitches, higher velocity is better
        return difference > 0

def is_spin_rate_better(difference, pitch_type):
    """Determine if spin rate difference is better based on pitch type"""
    
    # Normalize pitch type names for comparison
    pitch_type_lower = pitch_type.lower()
    
    # Define pitch categories where lower spin rate is better
    lower_spin_pitches = [
        'splitter', 'split-finger', 'splitfinger', 'split finger',
        'knuckleball', 'knuckle ball'  # knuckleballs also benefit from lower spin
    ]
    
    # Check for matches
    is_lower_spin_pitch = any(pattern in pitch_type_lower for pattern in lower_spin_pitches)
    
    if is_lower_spin_pitch:
        # For splitters and knuckleballs, lower spin rate is better
        return difference < 0
    else:
        # For all other pitches, higher spin rate is better
        return difference > 0

# Update the calculate_difference_from_average function to use spin rate logic
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
    elif metric_name == 'spin_rate' and pitch_type:  # Add this line
        better = is_spin_rate_better(difference, pitch_type)
    else:
        # For all other metrics, more is generally better
        better = difference > 0
    
    return {
        'difference': difference,
        'better': better,
        'absolute_diff': abs(difference)
    }

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
        
        # Calculate averages for each pitch type across ALL levels (for detailed comparison)
        multi_level_breakdown = []
        # Get the pitcher's comparison level from the first pitch data
        pitcher_name = pitch_data[0].get('Pitcher') if pitch_data else None
        pitcher_comparison_level = get_pitcher_competition_level(pitcher_name) if pitcher_name else 'D1'
        levels = ['D1', 'D2', 'D3']  # Keep all three levels for detailed comparison
        
        # Define priority order - Fastball first, then by general usage/importance
        priority_types = ['Fastball', 'Sinker', 'Cutter', 'Slider', 'Curveball', 'ChangeUp', 'Sweeper', 'Splitter', 'Knuckleball']
        
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
            
            # Get comparisons for ALL levels (D1, D2, D3)
            for level in levels:
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
                    college_averages['avg_spin_rate'] if college_averages else None,
                    metric_name='spin_rate',
                    pitch_type=pitch_type
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
                'level_comparisons': level_comparisons,
                'comparison_level': pitcher_comparison_level  # Add this for the summary table
            })
        
        return multi_level_breakdown

    except Exception as e:
        print(f"Error getting multi-level comparisons: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


# Add this new function to get pitcher's competition level from Info table
def get_pitcher_competition_level(pitcher_name):
    """Get the competition level for a specific pitcher from the Info table"""
    try:
        query = """
        SELECT Comp
        FROM `V1PBRInfo.Info`
        WHERE Prospect = @pitcher_name
        LIMIT 1
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pitcher_name", "STRING", pitcher_name),
            ]
        )
        
        result = client.query(query, job_config=job_config)
        row = list(result)
        
        if row and row[0].Comp:
            return row[0].Comp
        else:
            return 'D1'  # Default to D1 if no competition level found
            
    except Exception as e:
        print(f"Error getting competition level for {pitcher_name}: {str(e)}")
        return 'D1'  # Default to D1 on error

# Update the generate_pitcher_pdf function to automatically get comparison level
# Update the generate_pitcher_pdf function to include zone rate data
def generate_pitcher_pdf(pitcher_name, pitch_data, date, comparison_level=None):
    """Generate a PDF report for the pitcher using WeasyPrint with college comparisons and movement plot"""
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
        
        # Get competition level from Info table if not provided
        if comparison_level is None:
            comparison_level = get_pitcher_competition_level(pitcher_name)
            print(f"Retrieved competition level for {formatted_name}: {comparison_level}")
        
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
        
        # Define priority order - Fastball first, then by general usage/importance
        priority_types = ['Fastball', 'Sinker', 'Cutter', 'Slider', 'Curveball', 'ChangeUp', 'Sweeper', 'Splitter', 'Knuckleball']
        
        # Sort pitch types with priority
        sorted_pitch_types = []
        
        # Add priority types first (if they exist)
        for priority_type in priority_types:
            if priority_type in pitch_type_data:
                sorted_pitch_types.append(priority_type)
        
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
                           college_averages['avg_spin_rate'] if college_averages else None,
                           metric_name='spin_rate',
                           pitch_type=pitch_type)
            
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
        
        # Generate SVG plots with debugging
        print(f"Generating plots for {formatted_name}...")
        movement_plot_svg = generate_movement_plot_svg(pitch_data)
        pitch_location_plot_svg = generate_pitch_location_plot_svg(pitch_data)
        
        # Calculate zone rates
        zone_rate_data = calculate_zone_rates(pitch_data)
        
        # Debug plot generation
        print(f"Movement plot generated: {movement_plot_svg is not None}")
        print(f"Pitch location plot generated: {pitch_location_plot_svg is not None}")
        print(f"Zone rate data calculated: {zone_rate_data is not None}")
        
        if pitch_location_plot_svg:
            print(f"Pitch location SVG length: {len(pitch_location_plot_svg)} characters")
        else:
            print("Pitch location plot is None - checking data structure...")
            if pitch_data:
                sample_pitch = pitch_data[0]
                available_fields = list(sample_pitch.keys())
                print(f"Available fields in pitch data: {available_fields}")
                # Check for common location field variations
                location_fields = [field for field in available_fields if 'loc' in field.lower() or 'plate' in field.lower()]
                print(f"Location-related fields found: {location_fields}")
        
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
            multi_level_stats=multi_level_stats,
            movement_plot_svg=movement_plot_svg,
            pitch_location_plot_svg=pitch_location_plot_svg,
            zone_rate_data=zone_rate_data  # Add zone rate data to template
        )
        
        # Generate PDF using WeasyPrint with proper base_url for static files
        try:
            # Get the absolute path to the current directory so WeasyPrint can find static files
            base_url = f"file://{os.path.abspath('.')}/"
            print(f"Using base_url: {base_url}")
            
            # Check if static files exist
            static_dir = os.path.join(os.getcwd(), 'static')
            if not os.path.exists(static_dir):
                print(f"Warning: Static directory not found at {static_dir}")
                os.makedirs(static_dir, exist_ok=True)
                print(f"Created static directory at {static_dir}")
            
            # Check for required images
            required_images = ['pbr.png', 'miss.png']
            for image_name in required_images:
                image_path = os.path.join(static_dir, image_name)
                if os.path.exists(image_path):
                    print(f"Found image at: {image_path}")
                else:
                    print(f"Warning: Image not found at {image_path}")
            
            html_doc = weasyprint.HTML(string=rendered_html, base_url=base_url)
            pdf_bytes = html_doc.write_pdf()
            print(f"PDF generated successfully for {formatted_name}")
            return pdf_bytes
        except Exception as e:
            print(f"WeasyPrint error: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
        
    except Exception as e:
        print(f"Error generating PDF for {pitcher_name}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# Update the send_pitcher_email function to automatically get comparison level
def send_pitcher_email(pitcher_name, email, pitch_data, date, comparison_level=None):
    """Send email to pitcher with PDF attachment (using WeasyPrint)"""
    try:
        # Check if email config is available
        if not EMAIL_USERNAME or not EMAIL_PASSWORD:
            print("Email configuration not available. Please check email_config.json")
            return False
        
        # Get competition level from Info table if not provided
        if comparison_level is None:
            comparison_level = get_pitcher_competition_level(pitcher_name)
        
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

# Update the send_emails route to remove comparison_level parameter
@app.route('/api/send-emails', methods=['POST'])
def send_emails():
    """API endpoint to send emails to pitchers with their data"""
    if not client:
        return jsonify({'error': 'BigQuery client not initialized'}), 500
    
    try:
        data = request.get_json()
        selected_date = data.get('date')
        
        if not selected_date:
            return jsonify({'error': 'Date is required'}), 400
        
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
        SELECT Event, Prospect, Email, Type, Comp
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
                'event': row.Event,
                'comp': row.Comp or 'D1'  # Default to D1 if Comp is null
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
                    
                    # Try to send email with automatic competition level
                    comparison_level = prospect['comp']
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
                'match_rate_percentage': round((total_matched / total_prospects) * 100, 1) if total_prospects > 0 else 0
            },
            'sent_emails': sent_emails,
            'failed_emails': failed_emails,
            'unmatched_prospects': unmatched_prospects,
            'pitchers_in_test_table': pitchers_from_test
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Update the send_individual_email route to remove comparison_level parameter
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
        
        if not selected_date or not pitcher_name or not pitcher_email:
            return jsonify({'error': 'Date, pitcher name, and email are required'}), 400
        
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
        
        # Get competition level automatically
        comparison_level = get_pitcher_competition_level(pitcher_name)
        
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
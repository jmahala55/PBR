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
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch

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

def generate_pitcher_pdf(pitcher_name, pitch_data, date):
    """Generate a PDF report for the pitcher using ReportLab"""
    try:
        # Calculate summary stats
        if not pitch_data:
            print(f"No pitch data for {pitcher_name}")
            return None
            
        total_pitches = len(pitch_data)
        speeds = [p.get('RelSpeed', 0) for p in pitch_data if p.get('RelSpeed')]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0
        max_speed = max(speeds) if speeds else 0
        
        spin_rates = [p.get('SpinRate', 0) for p in pitch_data if p.get('SpinRate')]
        avg_spin = sum(spin_rates) / len(spin_rates) if spin_rates else 0
        
        pitch_types = list(set([p.get('TaggedPitchType', 'Unknown') for p in pitch_data if p.get('TaggedPitchType')]))
        
        print(f"Generating PDF for {pitcher_name} with {total_pitches} pitches")
        
        # Create a bytes buffer for the PDF
        buffer = io.BytesIO()
        
        # Create the PDF document
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        
        # Title
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=30,
            alignment=1  # Center alignment
        )
        title = Paragraph("⚾ Pitching Performance Report", title_style)
        story.append(title)
        
        # Player name and date
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Heading2'],
            fontSize=16,
            spaceAfter=20,
            alignment=1
        )
        subtitle = Paragraph(f"{pitcher_name} - {date}", subtitle_style)
        story.append(subtitle)
        
        story.append(Spacer(1, 12))
        
        # Summary section
        summary_style = ParagraphStyle(
            'Summary',
            parent=styles['Normal'],
            fontSize=12,
            spaceAfter=12
        )
        
        story.append(Paragraph("<b>Performance Summary</b>", styles['Heading3']))
        story.append(Paragraph(f"<b>Total Pitches:</b> {total_pitches}", summary_style))
        story.append(Paragraph(f"<b>Average Velocity:</b> {avg_speed:.1f} mph", summary_style))
        story.append(Paragraph(f"<b>Max Velocity:</b> {max_speed:.1f} mph", summary_style))
        story.append(Paragraph(f"<b>Average Spin Rate:</b> {avg_spin:.0f} rpm", summary_style))
        story.append(Paragraph(f"<b>Pitch Types:</b> {', '.join(pitch_types)}", summary_style))
        
        story.append(Spacer(1, 20))
        
        # Detailed pitch table
        story.append(Paragraph("<b>Detailed Pitch Data</b>", styles['Heading3']))
        story.append(Spacer(1, 12))
        
        # Create table data
        table_data = [['Pitch #', 'Type', 'Velocity (mph)', 'Spin Rate (rpm)', 'Vert Break (in)', 'Horz Break (in)']]
        
        for pitch in pitch_data:
            row = [
                str(pitch.get('PitchNo', 'N/A')),
                pitch.get('TaggedPitchType', 'N/A'),
                f"{pitch.get('RelSpeed', 0):.1f}" if pitch.get('RelSpeed') else 'N/A',
                f"{pitch.get('SpinRate', 0):.0f}" if pitch.get('SpinRate') else 'N/A',
                f"{pitch.get('InducedVertBreak', 0):.1f}" if pitch.get('InducedVertBreak') else 'N/A',
                f"{pitch.get('HorzBreak', 0):.1f}" if pitch.get('HorzBreak') else 'N/A'
            ]
            table_data.append(row)
        
        # Create and style the table
        table = Table(table_data, colWidths=[0.8*inch, 1*inch, 1*inch, 1*inch, 1*inch, 1*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        story.append(table)
        
        # Footer
        story.append(Spacer(1, 30))
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=10,
            alignment=1
        )
        story.append(Paragraph("Keep up the excellent work!", footer_style))
        story.append(Paragraph("Coaching Staff", footer_style))
        
        # Build the PDF
        doc.build(story)
        
        # Get the PDF data
        pdf_data = buffer.getvalue()
        buffer.close()
        
        print(f"PDF generated successfully for {pitcher_name}")
        return pdf_data
        
    except Exception as e:
        print(f"Error generating PDF for {pitcher_name}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def send_pitcher_email(pitcher_name, email, pitch_data, date):
    """Send email to pitcher with PDF attachment"""
    try:
        # Check if email config is available
        if not EMAIL_USERNAME or not EMAIL_PASSWORD:
            print("Email configuration not available. Please check email_config.json")
            return False
        
        # Generate PDF
        pdf_data = generate_pitcher_pdf(pitcher_name, pitch_data, date)
        if not pdf_data:
            print(f"Failed to generate PDF for {pitcher_name}")
            return False
        
        # Calculate basic stats for email body
        total_pitches = len(pitch_data) if pitch_data else 0
        
        # Create email content (simple text, PDF has the details)
        subject = f"Your Pitching Performance Report - {date}"
        
        body = f"""Hi {pitcher_name},

Your pitching performance report for {date} is attached as a PDF.

Report Summary:
- Total Pitches: {total_pitches}
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
        pdf_attachment.add_header(
            'Content-Disposition',
            f'attachment; filename="{pitcher_name.replace(", ", "_")}_Report_{date}.pdf"'
        )
        msg.attach(pdf_attachment)
        
        # Send email
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        print(f"Email with PDF sent successfully to {pitcher_name} at {email}")
        return True
        
    except Exception as e:
        print(f"Failed to send email to {pitcher_name} at {email}: {str(e)}")
        return False

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
                    
                    # Try to send email (actually send it)
                    print(f"Attempting to send email to {prospect_name} at {prospect['email']}")
                    email_success = send_pitcher_email(prospect_name, prospect['email'], pitch_data, selected_date)
                    print(f"Email result for {prospect_name}: {email_success}")
                    
                    if email_success:
                        sent_emails.append({
                            'pitcher': prospect_name,
                            'email': prospect['email'],
                            'type': prospect['type'],
                            'event': prospect['event'],
                            'pitch_count': len(pitch_data)
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

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    import os
    if not os.path.exists('templates'):
        os.makedirs('templates')
        print("Created templates directory")
    
    print("Starting Flask server...")
    print("Make sure harvard-baseball-13fab221b2d4.json is in the same directory")
    print("Make sure templates/index.html exists")
    app.run(debug=True, host='0.0.0.0', port=5000)
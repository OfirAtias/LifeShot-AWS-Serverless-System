import boto3
import json
import time
import urllib.parse
import math
from datetime import datetime

# --- הגדרות מערכת ---
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:452845599848:LifeguardAlerts" 
EVENTS_TABLE = 'LifeShot_Events'
TRACKING_TABLE = 'LifeShot_Tracking'

# ספי רגישות לזיהוי טביעה
MOVEMENT_THRESHOLD = 0.05       # תזוזה מינימלית כדי להיחשב שחייה
DROWNING_TIME_LIMIT = 30        # שניות ללא תזוזה לפני התראה
MAX_TIME_BETWEEN_FRAMES = 60    # איפוס מעקב אם עבר יותר מדי זמן

# אתחול שירותי ענן
rekognition = boto3.client('rekognition')
sns = boto3.client('sns')
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

def lambda_handler(event, context):
    print("DEBUG: Starting Analysis...")
    
    # טיפול בסימולציות ידניות (אופציונלי)
    if 'body' in event or 'action' in event:
        return {'statusCode': 200, 'body': json.dumps('Simulation Triggered')}

    # טריגר אמיתי מ-S3 בעת העלאת תמונה
    if 'Records' in event:
        try:
            bucket = event['Records'][0]['s3']['bucket']['name']
            key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'], encoding='utf-8')
            
            # שליחת התמונה ל-Rekognition לזיהוי אובייקטים
            response = rekognition.detect_labels(
                Image={'S3Object': {'Bucket': bucket, 'Name': key}},
                MaxLabels=10,
                MinConfidence=70
            )
            
            # סינון: חיפוש בני אדם בלבד
            persons_found = []
            for label in response['Labels']:
                if label['Name'] in ['Person', 'Human', 'Swimmer', 'Boy', 'Girl']:
                    for instance in label.get('Instances', []):
                        box = instance['BoundingBox']
                        center_x = box['Left'] + (box['Width'] / 2)
                        center_y = box['Top'] + (box['Height'] / 2)
                        
                        persons_found.append({
                            'x': center_x, 
                            'y': center_y,
                            'box': box
                        })

            # אם אין אנשים - איפוס מעקב
            if not persons_found:
                print("No people detected.")
                reset_tracking()
                return {'statusCode': 200, 'body': json.dumps('Pool Empty')}
            
            # ניתוח האדם הראשון שזוהה (לוגיקת תזוזה)
            current_person = persons_found[0]
            danger_detected = check_drowning_logic(current_person, key)
            
            if danger_detected:
                return {'statusCode': 200, 'body': json.dumps('DROWNING ALERT!')}
            else:
                return {'statusCode': 200, 'body': json.dumps('Swimmer Active - Safe')}

        except Exception as e:
            print(f"Error: {str(e)}")
            return {'statusCode': 500, 'body': json.dumps('Error')}

    return {'statusCode': 400, 'body': json.dumps('Unknown Trigger')}

# --- פונקציות לוגיקה ---

def check_drowning_logic(person_data, image_url):
    track_table = dynamodb.Table(TRACKING_TABLE)
    current_time = int(time.time())
    camera_id = 'cam-01' 
    
    # שליפת נתונים קודמים מהזיכרון
    response = track_table.get_item(Key={'camera_id': camera_id})
    
    # אם זה שחיין חדש לגמרי
    if 'Item' not in response:
        print("New swimmer detected.")
        save_state(person_data, current_time, current_time) 
        return False

    prev_data = response['Item']
    prev_x = float(prev_data['last_x'])
    prev_y = float(prev_data['last_y'])
    start_time = int(prev_data['start_timer'])
    last_seen_time = int(prev_data['last_seen'])

    # בדיקה אם הפער בין התמונות גדול מדי (סשן חדש)
    time_since_last_check = current_time - last_seen_time
    if time_since_last_check > MAX_TIME_BETWEEN_FRAMES:
        print(f"Session expired. Starting NEW timer.")
        save_state(person_data, current_time, current_time)
        return False
    
    # חישוב דלתא (מרחק תזוזה)
    movement_delta = math.sqrt((person_data['x'] - prev_x)**2 + (person_data['y'] - prev_y)**2)
    print(f"Swimmer moved: {movement_delta:.4f} units")

    if movement_delta > MOVEMENT_THRESHOLD:
        # יש תזוזה - הכל תקין, מאפסים טיימר
        print("Movement detected! Resetting timer.")
        save_state(person_data, current_time, current_time)
        return False
    else:
        # אין תזוזה - בודקים כמה זמן המצב נמשך
        time_elapsed = current_time - start_time
        print(f"No movement for {time_elapsed} seconds...")
        
        save_state(person_data, current_time, start_time)
        
        # אם עבר זמן הסף - התראה על טביעה
        if time_elapsed > DROWNING_TIME_LIMIT:
            print("!!! DROWNING DETECTED !!!")
            
            trigger_alert(
                zone=4, 
                score=98, 
                image_url=image_url,
                drowning_type="PASSIVE_DROWNING",
                movement_val=f"{movement_delta:.4f}", 
                cam_id=camera_id,
                box_data=person_data['box']
            )
            return True
            
    return False

def save_state(person_data, last_seen, start_time):
    # שמירת מצב נוכחי ב-DynamoDB
    track_table = dynamodb.Table(TRACKING_TABLE)
    track_table.put_item(Item={
        'camera_id': 'cam-01',
        'last_x': str(person_data['x']),
        'last_y': str(person_data['y']),
        'last_seen': str(last_seen),
        'start_timer': str(start_time)
    })

def reset_tracking():
    # מחיקת היסטוריית מעקב
    track_table = dynamodb.Table(TRACKING_TABLE)
    try:
        track_table.delete_item(Key={'camera_id': 'cam-01'})
    except:
        pass

def trigger_alert(zone, score, image_url, drowning_type, movement_val, cam_id, box_data):
    # יצירת רשומת אירוע ושליחת התראה
    event_id = f"EVT-{int(time.time())}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    item_data = {
        'eventId': event_id,
        'timestamp': str(int(time.time())),      
        'created_at': now_str,                   
        'zone': zone,
        'riskScore': score,
        'status': 'OPEN',
        'imageSource': image_url,
        'drowning_type': drowning_type,          
        'movement_delta_score': movement_val,    
        'camera_id': cam_id,                     
        'stream_status': 'HEALTHY',              
        'detected_object_metadata': str(box_data), 
        'notified_group': 'Lifeguards_Main_Shift'  
    }
    
    # שמירה בטבלה
    dynamodb.Table(EVENTS_TABLE).put_item(Item=item_data)
    
    # שליחת הודעה ל-SNS (אופציונלי אם מוגדר)
    if SNS_TOPIC_ARN and "YOUR_SNS_ARN" not in SNS_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Message=f"🚨 DROWNING ALERT!\nType: {drowning_type}\nDelta: {movement_val} (No Move)\nTime: {now_str}",
                Subject="⚠️ LIFESHOT EMERGENCY"
            )
        except:
            print("SNS Error or not configured properly")
            
    return event_id
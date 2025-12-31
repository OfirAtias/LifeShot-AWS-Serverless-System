import json
import boto3

# שם הבאקט לאחסון תמונות
BUCKET_NAME = 'lifeshot-pool-images'
s3 = boto3.client('s3')

def lambda_handler(event, context):
    # הגדרת כותרות CORS (חובה כדי שהדפדפן יאפשר את הבקשה)
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'OPTIONS,GET,PUT'
    }

    try:
        # קבלת שם הקובץ מהבקשה
        file_name = event.get('queryStringParameters', {}).get('filename', 'test.jpg')
        
        # יצירת כתובת URL זמנית ומאובטחת להעלאה ל-S3
        presigned_url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': BUCKET_NAME,
                'Key': file_name,
                'ContentType': 'image/jpeg'
            },
            ExpiresIn=60  # הלינק תקף לדקה אחת בלבד
        )
        
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({
                'upload_url': presigned_url,
                'file_name': file_name
            })
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps(f"Error: {str(e)}")
        }
import json
import boto3
from decimal import Decimal

# מחלקה להמרת מספרים עשרוניים של DynamoDB ל-JSON רגיל
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('LifeShot_Events')

def lambda_handler(event, context):
    # כותרות CORS לגישה מהדשבורד
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'OPTIONS,GET'
    }

    try:
        # קריאת כל האירועים מהטבלה
        response = table.scan()
        items = response['Items']
        
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps(items, cls=DecimalEncoder)
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps(f"Error: {str(e)}")
        }
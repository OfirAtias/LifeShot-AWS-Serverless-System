http://localhost:5500/client/pages/index.html

פתח POWERSHELL
CD לקובץ DEPLOY 

ודא שאתה מחובר ל-AWS:
aws sts get-caller-identity

להתחבר לסשן
aws configure

להתנתק מהSHELL 
Remove-Item $env:USERPROFILE\.aws\credentials
Remove-Item $env:USERPROFILE\.aws\config


להריץ את הסקריפט
python lifeshot_bootstrap.py


לנקות חשבון 
$env:DRY_RUN="false"
python lifeshot_cleanup.py

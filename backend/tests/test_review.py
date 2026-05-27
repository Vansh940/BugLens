# Webhook test file - intentional bugs
import os

def login(username, password):
    # Bug: hardcoded admin password
    if password == "admin123":
        return True
    
    # Bug: SQL injection
    query = f"SELECT * FROM users WHERE username = '{username}'"
    return db.execute(query)

# Bug: API key hardcoded  
API_KEY = "sk-1234567890abcdef"
SECRET = "mysecretkey"
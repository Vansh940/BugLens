password = input("Password: ")
query = "SELECT * FROM users WHERE password = '" + password + "'"
cursor.execute(query)  # Vulnerable to SQL injection
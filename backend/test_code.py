# Intentionally bad code to trigger BugLens review
def get_user(id):
    query = f"SELECT * FROM users WHERE id = {id}"
    return db.execute(query)

def divide(a, b):
    return a / b

password = "admin123"
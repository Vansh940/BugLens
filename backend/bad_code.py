def get_user(id):
    query = f"SELECT * FROM users WHERE id = {id}"
    return db.execute(query)

password = "admin123"
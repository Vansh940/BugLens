<?php

// Bug 1: SQL Injection
function getUser($id) {
    $query = "SELECT * FROM users WHERE id = " . $id;
    $result = mysqli_query($conn, $query);
    return $result;
}

// Bug 2: Hardcoded password
$db_password = "admin123";
$db_host = "localhost";

// Bug 3: No input validation
function deleteUser($id) {
    $query = "DELETE FROM users WHERE id = " . $id;
    mysqli_query($conn, $query);
}

// Bug 4: Password stored in plain text
function createUser($username, $password) {
    $query = "INSERT INTO users (username, password) 
              VALUES ('$username', '$password')";
    mysqli_query($conn, $query);
}

// Bug 5: Error suppression
$result = @file_get_contents("http://example.com/api");

// Bug 6: Weak comparison
if ($user_input == true) {
    echo "Admin access granted";
}
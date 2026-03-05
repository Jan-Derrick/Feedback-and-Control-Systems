#include <Servo.h>

Servo myServo;
const int servoPin = 9;

void setup() {
  myServo.attach(servoPin);
  Serial.begin(9600);
  // Set initial position
  myServo.write(0);
  Serial.println("System Ready. Enter degrees (0-180):");
}

void loop() {
  if (Serial.available() > 0) {
    // Read input until newline
    String inputString = Serial.readStringUntil('\n');
    inputString.trim(); // Remove whitespace
    
    if (inputString.length() > 0) {
      long val = inputString.toInt();
      
      // Validation Logic
      if (val == 0 && inputString != "0") {
          Serial.println("Error: Invalid input. Please enter a number.");
      } 
      else if (val >= 0 && val <= 180) {
        myServo.write(val);
        Serial.print("Success: Servo moved to ");
        Serial.print(val);
        Serial.println(" degrees.");
      } else {
        Serial.print("Error: ");
        Serial.print(val);
        Serial.println(" is out of range (0-180).");
      }
    }
  }
}
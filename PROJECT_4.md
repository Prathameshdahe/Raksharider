Project Planning Report
Two-Wheeler Driving Behaviour Detection System
Problem Statement
Two-wheeler riders frequently violate basic road-safety norms — riding without a helmet, triple-riding, and rash or reckless driving — creating safety risks for others on the road. Manual monitoring of such violations is inconsistent and largely reactive.
This project builds an AI-assisted driving-behaviour detection and reporting ecosystem, to be piloted within the college campus. A citizen-reporting mobile app lets users capture a photo or short video of a two-wheeler violation and select the violation type from predefined categories (no helmet, triple-riding, rash driving), while also reading the vehicle's number plate. An AI model reviews each submission to help decide whether it qualifies as a genuine, fineable violation before it reaches the admin web dashboard, where authorized personnel confirm the flag and issue a fine or notice, triggering an SMS to the concerned individual. Firebase is used for the backend and database.
Resources and Responsibilities
Team member
Responsibility
Prathamesh Dahe
Mobile app development (citizen reporting app); assists with AI model integration
Manamrit Singh
Database and backend development (Firebase setup, backend logic, data flow between app and dashboard)
Harsh Pal
AI model development (helmet, triple-riding and rash-driving detection, number-plate recognition)
Anushka Srivastava
Admin web dashboard development (report review, flag management, fine issuance)
Timeline
Phase
Work
Week 1–2
Finalize requirements, design app and admin dashboard wireframes, set up Firebase project and database structure.
Week 3–4
Build the basic mobile app (camera capture, predefined violation categories, submission flow) and connect it to Firebase.
Week 5–6
Develop the AI model for number-plate recognition and violation detection (helmet, triple-riding, rash driving).
Week 7
Integrate the AI model with the app submission pipeline for automated review of reports.
Week 8
Build the admin web dashboard for reviewing reports and confirming or rejecting flags.
Week 9
Integrate SMS notification service and connect admin actions to report status updates in the app.
Week 10
Testing, bug fixing, documentation and final demo preparation.
Expected Outcome
A working pilot system: a mobile app for reporting two-wheeler violations with photo/video evidence and automatic number-plate detection, an AI review layer that flags likely violations, and an admin web dashboard where staff confirm violations and issue fines with SMS notification to the offender. The project first focuses on a reliable basic pilot, after which features such as an appeal workflow, analytics, or a reporter trust score are added depending on available time and data.
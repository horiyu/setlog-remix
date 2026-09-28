# rooms/

One PNG per room you want to post to, named `<room name as shown on setlog's send screen>.png`.
Each is a 128x128 crop of that room's avatar from a screenshot of the send screen (1080x2400 AVD,
keyboard hidden): x 178..306, ±64 px around the row's centre line. `find_room.py` finds the row by
this picture, and `GET /rooms` lists the file names as the rooms a Shortcut can offer.

The PNGs are screenshots of your own rooms and their members' pictures, so they are not committed.

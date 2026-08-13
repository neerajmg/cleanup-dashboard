-- Cleanup Dashboard launcher applet (template).
-- Do not compile this file directly: run ./build_app.sh, which fills in
-- the __SERVER_SCRIPT__ placeholder with this project's location and
-- installs the compiled app into /Applications.
--
-- Double-click the app: starts the server (if not already running) and
-- opens the dashboard in the default browser. Stays in the Dock while
-- running. Quit (Cmd-Q or Dock right-click > Quit): stops the server.

property serverURL : "http://127.0.0.1:8765"
property serverScript : "__SERVER_SCRIPT__"
property tokenFile : "__PROJECT_DIR__/.session_token"

on serverRunning()
	try
		do shell script "/usr/sbin/lsof -ti tcp:8765"
		return true
	on error
		return false
	end try
end serverRunning

-- The server writes a per-session token file at startup; API calls without
-- the token are refused, so the URL we open must carry it.
on readToken()
	repeat with attempt from 1 to 4
		try
			return do shell script "/bin/cat " & quoted form of tokenFile
		on error
			delay 0.5
		end try
	end repeat
	return ""
end readToken

on startAndOpen()
	if not serverRunning() then
		-- A token file from a previous session may survive an unclean stop;
		-- remove it so readToken() can't grab a stale token before the new
		-- server writes its own.
		try
			do shell script "/bin/rm -f " & quoted form of tokenFile
		end try
		do shell script "/usr/bin/nohup /usr/bin/python3 " & quoted form of serverScript & " --no-browser > /dev/null 2>&1 &"
		delay 1
	end if
	set sessionToken to readToken()
	if sessionToken is "" then
		open location serverURL
	else
		open location serverURL & "/?token=" & sessionToken
	end if
end startAndOpen

on run
	startAndOpen()
end run

-- Clicking the Dock icon while already running re-opens the dashboard tab.
on reopen
	startAndOpen()
end reopen

-- Kill whatever owns the dashboard port, so quit works even for a server
-- that was started by hand from Terminal.
on quit
	try
		do shell script "/bin/kill $(/usr/sbin/lsof -ti tcp:8765) 2>/dev/null; exit 0"
	end try
	try
		do shell script "/bin/rm -f " & quoted form of tokenFile
	end try
	continue quit
end quit

local mp = require "mp"
local fired = false
local t0 = mp.get_time()
mp.register_event("file-loaded", function()
    if fired then return end
    fired = true
    mp.add_timeout(1, function()
        mp.msg.info("TEST: sending keypress Ctrl+S")
        mp.command("keypress Ctrl+S")
    end)
end)
mp.add_periodic_timer(1, function()
    for _, tr in ipairs(mp.get_property_native("track-list") or {}) do
        if tr.type == "sub" and tr.title == "subfix" then
            mp.msg.warn("TEST-RESULT loaded " .. tostring(tr["external-filename"]) .. " selected=" .. tostring(tr.selected))
            mp.command("quit 0")
        end
    end
    if mp.get_time() - t0 > 300 then mp.msg.error("TEST-RESULT timeout"); mp.command("quit 3") end
end)

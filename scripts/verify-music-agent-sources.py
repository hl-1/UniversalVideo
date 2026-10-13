"""Read-only real-source acceptance check; does not create application tasks or playlists."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.music_agent import platforms, validation
from backend import network


def main():
    costs = {}
    def charge(**items):
        for key,value in items.items():
            costs[key] = costs.get(key,0)+value
    network.request_guard.set(lambda:charge(network_requests=1))
    playlists = platforms.search_playlists("netease","华语 放松",1,charge)
    assert playlists["playlists"],"No real playlists returned"
    checked = []
    for playlist in playlists["playlists"][:2]:
        data = platforms.playlist_tracks("netease",playlist["playlist_id"],0,5,charge)
        for track in data["tracks"][:3]:
            lyrics = platforms.track_lyrics(track,charge)
            playback = validation.validate(track,charge,True)
            checked.append({"playlist_id":playlist["playlist_id"],"track":track,"lyric_source":lyrics["source"],
                            "lyrics_found":bool(lyrics["text"]) or lyrics["instrumental"] is True,"validation":playback})
            if checked[-1]["lyrics_found"] and playback["identity"] and playback["full"]:
                print(json.dumps({"passed":True,"checked":checked,"cost":costs},ensure_ascii=True))
                return
    print(json.dumps({"passed":False,"checked":checked,"cost":costs},ensure_ascii=True))
    raise SystemExit(1)


if __name__=="__main__":
    main()

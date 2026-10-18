#pragma once
// Real-life clock and weather (RealTime / RealWeather in SkateVLegacy.ini).
// RealTime holds GTA's clock at the PC's local time, ticking with it.
// RealWeather sets GTA's weather from the current weather where the player
// lives (Open-Meteo, no API key): WeatherLocation is "lat,lon", a city name
// (Open-Meteo's geocoder) or empty for the IP's location (ipwho.is). Fetched
// on a worker thread every WeatherRefresh minutes; GTA fades into the new
// weather over WeatherFade seconds. Offline, GTA keeps its own weather and
// the fetch is retried after a few minutes. Both stand aside during missions
// and cutscenes (GTA's scripts own the clock and the sky then).
//
// WeatherFor and JsonNumber are plain C++ so they can be checked without GTA.
#include <functional>
#include <string>

namespace realworld {

using LogFn = void (*)(const char*);

struct Options {
    bool time = false;
    bool weather = false;
    std::string location;     // WeatherLocation
    int refreshMinutes = 15;  // WeatherRefresh
    float fadeSeconds = 45;   // WeatherFade
};

// GTA weather type for a WMO weather code (Open-Meteo's weather_code), with
// cloud cover (%) and wind (km/h) to split clear/sunny and snow/blizzard.
const char* WeatherFor(int wmo, double cloudPercent, double windKmh);
// Plain English for a WMO code ("Light rain"), for the notification.
const char* Describe(int wmo);

// The number after "key": in `json`, searching from `from`; false when absent.
bool JsonNumber(const std::string& json, const char* key, double& out, std::size_t from = 0);

// `notify` shows a line on screen (a new weather fetched, the location not found).
void Start(LogFn log, std::function<void(const std::string&)> notify);
// Live from the menu: what is on, where, how often.
void Configure(const Options& o);
// Fetch the weather again now (menu action).
void Refresh();
// Last fetch in words ("Light rain, 8 C (Berlin) -> RAIN"), or why there is none.
std::string Status();
// Script thread, once per frame.
void Tick();

}  // namespace realworld

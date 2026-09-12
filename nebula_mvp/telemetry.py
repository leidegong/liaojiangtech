import math


class SimulatedFlight:
    def __init__(self):
        self.altitude = 30.0
        self.heading = 0.0
        self.latitude = 23.1200
        self.longitude = 113.2700
        self.elapsed = 0.0

    def step(self, control, dt, failsafe):
        self.elapsed += dt
        self.altitude = max(0, self.altitude + (control.throttle - .5) * 8 * dt)
        self.heading = (self.heading + control.yaw * 45 * dt) % 360
        speed = math.hypot(control.pitch, control.roll) * 12
        self.latitude += control.pitch * dt * .00005
        self.longitude += control.roll * dt * .00005
        return {"altitude": round(self.altitude, 2), "speed": round(speed, 2),
                "battery": round(max(0, 100 - self.elapsed / 60), 1),
                "latitude": round(self.latitude, 7), "longitude": round(self.longitude, 7),
                "roll": round(control.roll * 25, 1), "pitch": round(control.pitch * 25, 1),
                "yaw": round(self.heading, 1), "applied_control": control.public(),
                "failsafe": failsafe}

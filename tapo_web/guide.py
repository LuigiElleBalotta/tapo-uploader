"""The router guide shown to a user who adds a camera: manual steps and a text to paste into an AI assistant.

Nothing here contains the camera account or password: only the public network values the router needs.
"""
from dataclasses import dataclass

ASK_ADMIN = "(ask your administrator for the server's public IP address)"


@dataclass(frozen=True)
class Guide:
    manual_steps: list
    ai_prompt: str


def build_guide(settings, host, rtsp_port, onvif_port):
    server_ip = settings.server_public_ip or ASK_ADMIN
    manual = [
        "In the Tapo app open the camera, then Settings, Advanced, Camera Account, and create a username and "
        "password just for the camera. You will enter them on the camera page.",
        "In your router give the camera a fixed local IP address (a DHCP reservation), so the rules below keep working.",
        f"Make sure the name {host} always points to your home IP (a free dynamic DNS service does this). If your "
        "router shows a WAN IP different from the one you see on a site like ifconfig.me, you are behind CGNAT and "
        "port forwarding cannot work.",
        f"In the router's Port Forwarding section add rule 1: protocol TCP, external port {rtsp_port}, to the "
        f"camera's local IP, internal port 554. Allow it only from the remote IP {server_ip}.",
        f"Add rule 2: protocol TCP, external port {onvif_port}, to the camera's local IP, internal port 2020. "
        f"Allow it only from the remote IP {server_ip}.",
        "Save and apply. Some routers only activate a new rule after you apply a second time.",
        "Press Test connection on the camera page. Never open these ports to everyone: only the server's IP.",
    ]
    prompt = (
        "I need to set up port forwarding on my home router so a server can reach my IP camera.\n"
        "My router model: [your router model]\n"
        f"The camera's local IP address: [the camera's local IP]\n"
        f"My home's public address (dynamic DNS name): {host}\n"
        f"The server's public IP address: {server_ip}\n\n"
        "Please explain, step by step and for my router model, how to create these two port forwarding rules:\n"
        f"1. Protocol TCP, external (WAN) port {rtsp_port} -> the camera's local IP, internal port 554.\n"
        f"2. Protocol TCP, external (WAN) port {onvif_port} -> the camera's local IP, internal port 2020.\n"
        f"Both rules must accept connections ONLY from the remote IP {server_ip}, not from the whole internet "
        "(look for a 'remote host', 'source IP' or 'allowed IP' field). Also explain how to give the camera a fixed "
        "local IP, and how to check afterwards that the ports are closed to everyone else."
    )
    return Guide(manual_steps=manual, ai_prompt=prompt)

"""AFE Layer-4 execution motor (Zone B).

Executes only orders carrying a valid Aegis attestation, routes them with a
toxicity-aware smart order router, and submits them through a PAPER-ONLY broker
client by default. See ``motor.ExecutionMotor``.
"""

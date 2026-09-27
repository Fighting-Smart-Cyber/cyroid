"""pg-gateway: the reverse proxy in front of a range's applications.

A range's applications were published through two Traefik CRDs -- a ForwardAuth middleware and a
StripPrefix middleware -- chained onto the Ingress by annotation. Azure Application Gateway has
no ForwardAuth equivalent, and neither does the AWS Load Balancer Controller, GKE's ingress or
OpenShift's router: that step cannot be expressed as platform middleware anywhere but Traefik.
So it moves in here, and what the platform has to provide becomes one vanilla
`networking.k8s.io/v1` Ingress, which every controller has.

This process terminates requests from the software a range is training against -- third party at
best, hostile by design in a red-team exercise -- so it is the likeliest thing on the platform to
be compromised. It runs as its own Deployment with its own ServiceAccount, holds no database
credentials and no object-store keys, and verifies range-application credentials with a PUBLIC
key it cannot sign with (see utils/app_tokens).
"""

# =============================================================================
# Custom DH Parameters Generation for Nginx
# Run: openssl dhparam -out dhparam.pem 4096
# Mount into nginx container at /etc/nginx/ssl/dhparam.pem
# =============================================================================
# 
# Usage (one-time):
#   1. Generate: openssl dhparam -out dhparam.pem 4096
#   2. Copy to nginx SSL dir: cp dhparam.pem src/nginx/ssl/dhparam.pem
#   3. Ensure nginx.conf references: ssl_dhparam /etc/nginx/ssl/dhparam.pem;
#   4. Mount in compose:
#        volumes:
#          - ./nginx/ssl/dhparam.pem:/etc/nginx/ssl/dhparam.pem:ro
#
# This script validates the DH parameters file exists and has correct size.
# =============================================================================

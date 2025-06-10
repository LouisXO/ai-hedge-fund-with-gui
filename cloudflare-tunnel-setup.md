# Cloudflare Tunnel Setup for AI Hedge Fund Backend

## Step 1: Install Cloudflare Tunnel (cloudflared)

### On macOS:
```bash
# Install using Homebrew
brew install cloudflare/cloudflare/cloudflared

# Or download directly
curl -L --output cloudflared.pkg https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.pkg
sudo installer -pkg cloudflared.pkg -target /
```

## Step 2: Authenticate with Cloudflare

```bash
# This will open a browser window to log into your Cloudflare account
cloudflared tunnel login
```

## Step 3: Create a Tunnel

```bash
# Create a new tunnel (replace 'ai-hedge-fund-api' with your preferred name)
cloudflared tunnel create ai-hedge-fund-api

# This will create a tunnel and give you a tunnel ID
# Note down the tunnel ID - you'll need it
```

## Step 4: Create Tunnel Configuration

Create a config file at `~/.cloudflared/config.yml`:

```yaml
tunnel: ai-hedge-fund-api  # Your tunnel name
credentials-file: ~/.cloudflared/YOUR_TUNNEL_ID.json  # Replace with actual tunnel ID

ingress:
  # Route your API domain to your local backend
  - hostname: your-api-domain.your-domain.com  # Replace with your desired subdomain
    service: http://localhost:8000
  # Catch-all rule (required)
  - service: http_status:404
```

## Step 5: Configure DNS

```bash
# Create DNS record pointing to your tunnel
# Replace 'your-api-domain.your-domain.com' with your desired API subdomain
cloudflared tunnel route dns ai-hedge-fund-api your-api-domain.your-domain.com
```

## Step 6: Run the Tunnel

```bash
# Test the tunnel
cloudflared tunnel run ai-hedge-fund-api

# To run in background (for production)
cloudflared tunnel --config ~/.cloudflared/config.yml run
```

## Step 7: Install as a Service (Optional)

```bash
# Install as a system service to auto-start
sudo cloudflared service install
```

## Example URLs

After setup, your API will be accessible at:
- `https://your-api-domain.your-domain.com/portfolio/`
- `https://your-api-domain.your-domain.com/hedge-fund/run`
- etc.

## Troubleshooting

- Check tunnel status: `cloudflared tunnel list`
- View logs: `cloudflared tunnel run ai-hedge-fund-api --loglevel debug`
- Test locally first: Ensure your backend runs on `http://localhost:8000` 
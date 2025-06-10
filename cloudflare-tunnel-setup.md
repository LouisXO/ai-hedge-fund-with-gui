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
# Create a new tunnel
cloudflared tunnel create ai-hedge-fund-backend

# This will create a tunnel and give you a tunnel ID
# Note down the tunnel ID - you'll need it
```

## Step 4: Create Tunnel Configuration

Create a config file at `~/.cloudflared/config.yml`:

```yaml
tunnel: 0d96dc93-711a-421f-a722-42e47b03b8ea
credentials-file: /Users/louisleng/.cloudflared/0d96dc93-711a-421f-a722-42e47b03b8ea.json

ingress:
  # Route your API domain to your local backend
  - hostname: api.hedge-fund.louisleng.com
    service: http://localhost:8000
  # Catch-all rule (required)
  - service: http_status:404
```

## Step 5: Configure DNS

```bash
# Create DNS record pointing to your tunnel
cloudflared tunnel route dns ai-hedge-fund-backend api.hedge-fund.louisleng.com
```

## Step 6: Run the Tunnel

```bash
# Test the tunnel
cloudflared tunnel run ai-hedge-fund-backend

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
- `https://api.hedge-fund.louisleng.com/portfolio/`
- `https://api.hedge-fund.louisleng.com/hedge-fund/run`
- etc.

## Troubleshooting

- Check tunnel status: `cloudflared tunnel list`
- View logs: `cloudflared tunnel run ai-hedge-fund-backend --loglevel debug`
- Test locally first: Ensure your backend runs on `http://localhost:8000` 
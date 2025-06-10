# AI Hedge Fund Deployment Guide

## Frontend Deployment on Netlify

### Step 1: Update API Configuration

Before deploying, you need to:

1. **Replace the placeholder domain** in `app/frontend/src/config/api.ts`:
   ```typescript
   const CLOUDFLARE_TUNNEL_DOMAIN = 'your-actual-api-domain.your-domain.com';
   ```

2. **Or use environment variables** (recommended):
   - In Netlify dashboard, go to Site settings > Environment variables
   - Add: `VITE_API_BASE_URL` = `https://your-api-domain.your-domain.com`

### Step 2: Netlify Configuration

Create `app/frontend/netlify.toml`:

```toml
[build]
  base = "app/frontend"
  command = "npm run build"
  publish = "dist"

[build.environment]
  NODE_VERSION = "18"

[[redirects]]
  from = "/*"
  to = "/index.html"
  status = 200

[context.production.environment]
  VITE_API_BASE_URL = "https://your-api-domain.your-domain.com"

[context.deploy-preview.environment]
  VITE_API_BASE_URL = "https://your-api-domain.your-domain.com"
```

### Step 3: Deploy to Netlify

1. **Connect your repository** to Netlify
2. **Set build settings**:
   - Base directory: `app/frontend`
   - Build command: `npm run build`
   - Publish directory: `app/frontend/dist`
3. **Add environment variables** in Netlify dashboard
4. **Deploy**

## Backend Setup with Cloudflare Tunnel

### Required Steps:

1. **Install cloudflared** on your backend Mac (see cloudflare-tunnel-setup.md)

2. **Update backend CORS settings** to allow your Netlify domain:
   ```python
   # In your FastAPI app
   from fastapi.middleware.cors import CORSMiddleware
   
   app.add_middleware(
       CORSMiddleware,
       allow_origins=[
           "http://localhost:5173",  # Development
           "https://your-netlify-app.netlify.app",  # Production
           "https://your-custom-domain.com",  # Custom domain
       ],
       allow_credentials=True,
       allow_methods=["*"],
       allow_headers=["*"],
   )
   ```

3. **Create tunnel configuration**:
   ```yaml
   # ~/.cloudflared/config.yml
   tunnel: ai-hedge-fund-api
   credentials-file: ~/.cloudflared/YOUR_TUNNEL_ID.json
   
   ingress:
     - hostname: api.yourdomain.com  # Your API domain
       service: http://localhost:8000
     - service: http_status:404
   ```

4. **Start your services**:
   ```bash
   # Terminal 1: Start your FastAPI backend
   cd /path/to/your/backend
   python -m uvicorn main:app --host 0.0.0.0 --port 8000
   
   # Terminal 2: Start Cloudflare tunnel
   cloudflared tunnel run ai-hedge-fund-api
   ```

## Security Considerations

### 1. Environment Variables
- Never commit API URLs to git
- Use Netlify environment variables for production URLs
- Keep development URLs in local `.env` files

### 2. CORS Configuration
- Only allow specific origins in production
- Don't use wildcard (`*`) for origins in production

### 3. API Security
- Consider adding API keys or authentication
- Implement rate limiting
- Use HTTPS everywhere (Cloudflare tunnel provides this)

## Testing Your Deployment

### 1. Test API Accessibility
```bash
# Test your Cloudflare tunnel
curl https://your-api-domain.your-domain.com/portfolio/

# Should return your portfolio data or appropriate error
```

### 2. Test Frontend-Backend Connection
1. Open your Netlify deployed app
2. Check browser dev tools network tab
3. Verify API calls are going to your Cloudflare tunnel domain
4. Test portfolio creation, analysis, etc.

## Troubleshooting

### Common Issues:

1. **CORS Errors**: Update backend CORS origins
2. **API Not Found**: Check Cloudflare tunnel is running
3. **Build Errors**: Remove unused components (like PortfolioChart)
4. **Environment Variables**: Ensure VITE_API_BASE_URL is set correctly

### Debug Commands:
```bash
# Check tunnel status
cloudflared tunnel list

# Test tunnel locally
curl http://localhost:8000/portfolio/
curl https://your-api-domain.your-domain.com/portfolio/

# Check Netlify build logs
npm run build  # Locally first
```

## Example Final URLs

- **Frontend**: `https://your-app.netlify.app`
- **Backend API**: `https://api.yourdomain.com`
- **Example API call**: `https://api.yourdomain.com/portfolio/` 
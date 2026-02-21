# Cloudflare Setup for skchat.io

## DNS Records (for GitHub Pages)

Add these in Cloudflare DNS → skchat.io:

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| A | @ | 185.199.108.153 | DNS only |
| A | @ | 185.199.109.153 | DNS only |
| A | @ | 185.199.110.153 | DNS only |
| A | @ | 185.199.111.153 | DNS only |
| CNAME | www | smilinTux.github.io | DNS only |

## Email Routing

In Cloudflare → Email Routing → skchat.io:

1. Enable Email Routing
2. Add the following routing rules:

| From | Forward to |
|------|-----------|
| info@skchat.io | luminaSK@smilintux.org |
| support@skchat.io | luminaSK@smilintux.org |
| security@skchat.io | luminaSK@smilintux.org |
| contact@skchat.io | luminaSK@smilintux.org |
| hello@skchat.io | luminaSK@smilintux.org |
| team@skchat.io | luminaSK@smilintux.org |
| partnerships@skchat.io | luminaSK@smilintux.org |
| press@skchat.io | luminaSK@smilintux.org |
| admin@skchat.io | luminaSK@smilintux.org |
| api@skchat.io | luminaSK@smilintux.org |
| dev@skchat.io | luminaSK@smilintux.org |
| docs@skchat.io | luminaSK@smilintux.org |
| community@skchat.io | luminaSK@smilintux.org |
| newsletter@skchat.io | luminaSK@smilintux.org |

3. Cloudflare will auto-add the MX and TXT records for email routing.
4. Verify `luminaSK@smilintux.org` as destination address if not already verified.

## HTTPS

After DNS propagation, go to GitHub → skchat-io repo → Settings → Pages:
1. Verify custom domain: skchat.io
2. Enable "Enforce HTTPS"

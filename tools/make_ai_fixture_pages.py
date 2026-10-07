"""Write tests/fixtures/pages/*.html: fictional businesses in realistic page chrome.

The Phase 5 page-preparation and self-healing-selector tests read these pages.
Every business, domain (.example) and phone number (US 555-01xx, India
98200 00xxx) is fictional; the chrome (inline CSS, tag-manager scripts, JSON
config, menus, cookie banners, pop-ups, SVG, ads, JSON-LD) is what real sites
carry. Usage: python tools/make_ai_fixture_pages.py tests/fixtures/pages
"""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

def css(prefix, n=160):
    rules = []
    for i in range(n):
        rules.append(f".{prefix}-block-{i}{{margin:{i%7}px {i%5}px;padding:{i%9}px;color:#{(i*4011)%0xffffff:06x};"
                     f"font:400 {12+i%6}px/1.{i%9} 'Inter',Arial,sans-serif;border-radius:{i%4}px;"
                     f"transition:all .{i%9}s ease-in-out}}@media (max-width:{600+i}px){{.{prefix}-block-{i}{{display:{'block' if i%2 else 'flex'}}}}}")
    return "\n".join(rules)

def js(name, n=60):
    lines = [f"window.{name}=window.{name}||[];function gtag(){{{name}.push(arguments)}};gtag('js',new Date());"]
    for i in range(n):
        lines.append(f"(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':new Date().getTime(),event:'gtm.js{i}'}});"
                     f"var f=d.getElementsByTagName(s)[0],j=d.createElement(s);j.async=true;j.src='https://tags.example/gtm.js?id='+i+'&v={i}';f.parentNode.insertBefore(j,f);}})(window,document,'script','dataLayer','GTM-{i:04d}');")
    return "\n".join(lines)

CONFIG = json.dumps({"theme": {"palette": [f"#{i*99991%0xffffff:06x}" for i in range(80)],
                               "breakpoints": list(range(320, 1920, 40))},
                     "i18n": {f"key_{i}": f"Translated string number {i} for the widget" for i in range(150)}})

def nav(items):
    return ('<nav class="main-navigation" role="navigation"><ul class="menu">'
            + "".join(f'<li class="menu-item menu-item-{i}"><a href="/{s.lower().replace(" ", "-")}/">{s}</a>'
                      f'<ul class="sub-menu">' + "".join(f'<li><a href="/{s.lower()}/{j}/">{s} option {j}</a></li>' for j in range(5)) + "</ul></li>"
                      for i, s in enumerate(items))
            + "</ul></nav>")

COOKIE = ('<div id="cookie-consent" class="cookie-banner modal"><p>We use cookies to improve your experience, '
          'analyse traffic and personalise content. By clicking "Accept all" you consent to our use of cookies. '
          'Read our cookie policy for details on how we use them and how you can change your settings.</p>'
          '<button>Accept all</button><button>Reject</button><button>Settings</button></div>')
NEWSLETTER = ('<div class="newsletter-popup modal" aria-hidden="true"><h3>Subscribe to our newsletter</h3>'
              '<p>Get offers and news delivered to your inbox every month.</p><form><input type="email" name="email" '
              'placeholder="Your email"><button>Subscribe</button></form></div>')
SVG = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="' + " ".join(f"M{i} {i*2} L{i+3} {i+5} Z" for i in range(120)) + '"/></svg>'

def head(title, prefix):
    meta = "".join(f'<meta name="x-meta-{i}" content="value {i} for crawlers and social cards">' for i in range(25))
    links = "".join(f'<link rel="preload" href="/wp-content/themes/site/assets/font-{i}.woff2" as="font" crossorigin>' for i in range(12))
    return (f"<head><meta charset='utf-8'><title>{title}</title>{meta}{links}"
            f"<style>{css(prefix)}</style>"
            f"<script>{js('dataLayer')}</script>"
            f"<script id='theme-config' type='application/json'>{CONFIG}</script>"
            f"<script>window.__INITIAL_STATE__={CONFIG};</script></head>")

# 1. A dentist's homepage (US): contacts in the header bar, footer and JSON-LD.
jsonld = {"@context": "https://schema.org", "@type": "Dentist", "name": "Lakeshore Family Dental",
          "telephone": "+1-512-555-0142", "email": "hello@lakeshoredental.example",
          "url": "https://lakeshoredental.example/",
          "address": {"@type": "PostalAddress", "streetAddress": "4100 Lakeshore Blvd, Suite 210",
                      "addressLocality": "Austin", "addressRegion": "TX", "postalCode": "78701",
                      "addressCountry": "US"},
          "openingHours": "Mo-Fr 08:00-18:00", "priceRange": "$$",
          "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "reviewCount": "212"}}
services = "".join(f'<div class="service-card svc-block-{i}">{SVG}<h3>Service {i}: {s}</h3><p>{s} for the whole family, '
                   f'with gentle care, modern equipment and flexible appointment times that suit busy schedules.</p>'
                   f'<a class="btn" href="/services/{i}/">Learn more</a></div>'
                   for i, s in enumerate(["Check-ups and cleaning", "Teeth whitening", "Invisalign", "Dental implants",
                                          "Emergency care", "Pediatric dentistry", "Crowns and bridges", "Root canals"]))
testimonials = "".join(f'<blockquote class="testimonial"><p>"Patient story {i}: friendly staff, painless visit, and they '
                       f'explained everything clearly. Highly recommended!"</p><cite>Patient {i}</cite></blockquote>' for i in range(10))
home = (f"<!doctype html><html lang='en'>{head('Lakeshore Family Dental | Dentist in Austin, TX', 'lfd')}<body class='home page-template'>"
        f"{COOKIE}{NEWSLETTER}"
        "<div class='topbar'><span>Call us: <a href='tel:+15125550142'>(512) 555-0142</a></span>"
        "<span>Mon-Fri 8am-6pm</span></div>"
        f"<header class='site-header'><a class='logo' href='/'>{SVG}Lakeshore Family Dental</a>"
        f"{nav(['Home', 'About', 'Services', 'New Patients', 'Insurance', 'Blog', 'Contact'])}</header>"
        "<div class='breadcrumb'><a href='/'>Home</a> &raquo; Welcome</div>"
        "<main><section class='hero'><h1>Lakeshore Family Dental</h1><p>Family and cosmetic dentistry in downtown Austin "
        "since 2009. Same-week appointments for new patients.</p><a class='btn' href='/book/'>Book online</a></section>"
        f"<section class='services'><h2>Our services</h2>{services}</section>"
        f"<section class='testimonials'><h2>What patients say</h2>{testimonials}</section></main>"
        "<footer class='site-footer'><div class='footer-contact'><h4>Visit us</h4>"
        "<address>4100 Lakeshore Blvd, Suite 210, Austin, TX 78701</address>"
        "<p>Phone: <a href='tel:+15125550142'>(512) 555-0142</a></p>"
        "<p>Email: <a href='mailto:hello@lakeshoredental.example'>hello@lakeshoredental.example</a></p></div>"
        f"{nav(['Privacy', 'Terms', 'Accessibility', 'Sitemap'])}"
        "<div class='social-share'><a href='https://www.facebook.com/lakeshoredental.example'>Facebook</a>"
        "<a href='https://www.instagram.com/lakeshoredental_example/'>Instagram</a></div>"
        "<p class='copyright'>&copy; 2026 Lakeshore Family Dental</p></footer>"
        f"<script type='application/ld+json'>{json.dumps(jsonld)}</script>"
        f"<script>{js('pixelQueue', 40)}</script></body></html>")
(OUT / "clinic_home.html").write_text(home, encoding="utf-8")

# 2. A caterer's contact page (India): phone, obfuscated email, address in prose.
contact = (f"<!doctype html><html lang='en'>{head('Contact | Saffron Leaf Caterers, Pune', 'slc')}<body>{COOKIE}"
           f"<header>{nav(['Home', 'Menus', 'Weddings', 'Corporate', 'Gallery', 'Contact'])}</header>"
           "<main><h1>Contact Saffron Leaf Caterers</h1>"
           "<p>For weddings, corporate lunches and private parties anywhere in Pune, talk to our events team.</p>"
           "<div class='contact-grid'><div><h4>Call</h4><p><a href='tel:+919820000417'>+91 98200 00417</a></p></div>"
           "<div><h4>Write</h4><p>events [at] saffronleaf [dot] example</p></div>"
           "<div><h4>Kitchen</h4><p>Shop 4, Baner Road, Baner, Pune 411045</p></div></div>"
           "<form class='enquiry-form'><input name='name'><input name='phone'><textarea name='message'></textarea>"
           "<button>Send enquiry</button></form>"
           f"<section class='gallery'>{''.join(f'<figure><img src=/img/{i}.jpg alt=\"Buffet setup {i}\"><figcaption>Event {i}</figcaption></figure>' for i in range(30))}</section></main>"
           f"<footer>{nav(['Privacy', 'Refunds', 'Careers'])}<p>&copy; Saffron Leaf Caterers</p></footer>"
           f"<script>{js('dataLayer', 50)}</script></body></html>")
(OUT / "caterer_contact.html").write_text(contact, encoding="utf-8")

# 3-5. A business directory: two pages of one layout, then the same site redesigned.
DENTISTS_1 = [("Bright Smile Dental Studio", "98200 00101", "Office 2, Baner Road, Baner, Pune", "brightsmile.example"),
              ("Lotus Family Dentistry", "98200 00102", "12 Aundh Road, Aundh, Pune", "lotusdentistry.example"),
              ("Riverside Dental Care", "98200 00103", "Plot 7, Kothrud, Pune", None),
              ("Pearl Orthodontics", "98200 00104", "3rd Floor, FC Road, Shivajinagar, Pune", "pearlortho.example"),
              ("Sunrise Dental Clinic", "98200 00105", "Lane 5, Koregaon Park, Pune", None),
              ("Tooth Fairy Kids Dentist", "98200 00106", "Viman Nagar, Pune", "toothfairykids.example"),
              ("City Smiles Dental", "98200 00107", "MG Road, Camp, Pune", "citysmiles.example"),
              ("Hillside Dental Practice", "98200 00108", "Pashan Road, Pashan, Pune", None)]
DENTISTS_2 = [("Ivory Dental Lounge", "98200 00109", "Hadapsar, Pune", "ivorylounge.example"),
              ("Crescent Dental Centre", "98200 00110", "Wakad, Pune", None),
              ("Neem Tree Dental", "98200 00111", "Baner, Pune", "neemtreedental.example"),
              ("Smile Craft Clinic", "98200 00112", "Kharadi, Pune", None),
              ("Gentle Care Dentistry", "98200 00113", "Hinjewadi Phase 1, Pune", "gentlecare.example"),
              ("Modern Dental Studio", "98200 00114", "Deccan Gymkhana, Pune", None)]

def v1_card(i, name, phone, address, site):
    slug = name.lower().replace(" ", "-")
    return (f"<div class='listing-card' data-id='{1000+i}'>"
            f"<div class='card-media'><img src='/thumbs/{slug}.jpg' alt='{name}'></div>"
            f"<div class='card-body'><h3 class='biz-name'><a href='/pune/dentists/{slug}/'>{name}</a></h3>"
            f"<div class='rating'><span class='stars'>&#9733;&#9733;&#9733;&#9733;</span> <span>4.{i%10} ({20+i} reviews)</span></div>"
            f"<div class='biz-address'>{address}</div>"
            f"<div class='biz-contact'><span class='biz-phone'>{phone}</span>"
            + (f" <a class='biz-website' href='https://{site}/' rel='nofollow'>Website</a>" if site else "")
            + "</div><div class='tags'><span>Dentist</span><span>Open now</span></div></div>"
            "<div class='card-actions'><button>Get quote</button><button>Share</button></div></div>")

def directory_v1(page, entries, nxt):
    ads = "".join(f"<div class='ad-slot'><p>Sponsored: advertise your business here {i}</p></div>" for i in range(3))
    cards = "".join(v1_card(i + page * 10, *e) for i, e in enumerate(entries))
    pager = (f"<div class='pagination'><a href='/pune/dentists/?page={page-1}'>Prev</a> <span>Page {page}</span> "
             + (f"<a rel='next' href='/pune/dentists/?page={page+1}'>Next</a>" if nxt else "") + "</div>")
    return (f"<!doctype html><html lang='en'>{head(f'Dentists in Pune - Page {page} | Pune Business Pages', 'pbp')}<body>"
            f"{COOKIE}<header class='site-header'>{nav(['Home', 'Categories', 'Cities', 'Add your business', 'Login'])}"
            "<form class='search-form'><input name='q'><button>Search</button></form></header>"
            "<div class='breadcrumb'><a href='/'>Home</a> &gt; <a href='/pune/'>Pune</a> &gt; Dentists</div>"
            f"<main><h1>Dentists in Pune</h1><p class='result-count'>Showing {len(entries)} dentists</p>{ads}"
            f"<section class='results'>{cards}</section>{pager}</main>"
            f"<aside class='sidebar'>{nav(['Popular: Caterers', 'Popular: Plumbers', 'Popular: Salons'])}</aside>"
            f"<footer>{nav(['About', 'Contact', 'Terms', 'Privacy'])}<p>&copy; Pune Business Pages</p></footer>"
            f"<script>{js('dataLayer', 45)}</script></body></html>")

(OUT / "directory_v1_page1.html").write_text(directory_v1(1, DENTISTS_1, True), encoding="utf-8")
(OUT / "directory_v1_page2.html").write_text(directory_v1(2, DENTISTS_2, False), encoding="utf-8")

def v2_card(i, name, phone, address, site):
    slug = name.lower().replace(" ", "-")
    return (f"<article class='provider'><header class='provider__head'>"
            f"<h2 class='provider__title'><a href='/p/{slug}'>{name}</a></h2></header>"
            f"<p class='provider__location'>{address}</p>"
            f"<a class='provider__call' data-role='call' href='tel:+91{phone.replace(' ', '')}'>{phone}</a>"
            + (f"<a class='provider__site' href='https://{site}/'>Visit site</a>" if site else "")
            + "</article>")

def redesign(page, entries, nxt):
    more = (f"<a class='more' rel='next' href='/pune/dentists?p={page+1}'>More dentists</a>"
            if nxt else "")
    return (f"<!doctype html><html lang='en'>{head('Pune dentists | Pune Business Pages', 'pbp2')}<body>{COOKIE}"
            f"<div id='app'><header>{nav(['Explore', 'Cities', 'For business'])}</header>"
            "<main class='grid'><h1>Dentists near you in Pune</h1>"
            + "".join(v2_card(i, *e) for i, e in enumerate(entries))
            + f"</main>{more}</div>"
            f"<script>{js('dataLayer', 45)}</script></body></html>")

(OUT / "directory_v2_page1.html").write_text(redesign(1, DENTISTS_1, True), encoding="utf-8")
(OUT / "directory_v2_page2.html").write_text(redesign(2, DENTISTS_2, False), encoding="utf-8")
print("written", sorted(p.name for p in OUT.glob("*.html")))

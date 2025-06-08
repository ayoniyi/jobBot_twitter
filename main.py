import tweepy
import os
import time
import logging
from datetime import datetime, timedelta
import sqlite3
import re
from typing import List, Dict, Optional
import schedule
import threading
from dataclasses import dataclass
from ratelimit import limits, sleep_and_retry
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class JobPost:
    tweet_id: str
    author_username: str
    author_followers: int
    account_age_days: int
    tweet_text: str
    created_at: datetime
    matching_keywords: List[str]

class TwitterJobBot:
    def __init__(self):
        # Twitter API credentials
        self.bearer_token = os.getenv('TWITTER_BEARER_TOKEN')
        self.api_key = os.getenv('TWITTER_API_KEY')
        self.api_secret = os.getenv('TWITTER_API_SECRET')
        self.access_token = os.getenv('TWITTER_ACCESS_TOKEN')
        self.access_token_secret = os.getenv('TWITTER_ACCESS_TOKEN_SECRET')
        
        # Notification settings
        self.notification_webhook = os.getenv('NOTIFICATION_WEBHOOK_URL')  # Optional webhook
        self.your_twitter_id = os.getenv('YOUR_TWITTER_USER_ID')  # For DMs
        
        # Job search criteria
        self.keywords = [
            "hiring frontend",
            "frontend engineers",
            "new frontend",
            "apply frontend",
            "frontend developer",
            "frontend developers",
            "frontend engineer",
            "hiring react developer",
            "hiring javascript developer",
            "frontend position",
            "frontend role",
            "react role",
            "frontend job",
            "frontend opening",
            "frontend vacancy"
        ]
        
        # Additional filtering criteria
        self.min_followers = 999
        self.min_account_age_days = 365
        self.excluded_words = [
            "not hiring",
            "closed",
            "filled",
            "expired",
            "canceled",
            "cancelled"
        ]
        
        # Initialize Twitter clients
        self._init_twitter_clients()
        
        # Initialize database
        self._init_database()
        
        # Rate limiting tracking
        self.last_search_time = datetime.now() - timedelta(minutes=16)
        
    def _init_twitter_clients(self):
        """Initialize Twitter API clients"""
        try:
            # Client for API v2 (search, user lookup)
            self.client = tweepy.Client(
                bearer_token=self.bearer_token,
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
                wait_on_rate_limit=True
            )
            
            # API v1.1 for additional features if needed
            auth = tweepy.OAuth1UserHandler(
                self.api_key,
                self.api_secret,
                self.access_token,
                self.access_token_secret
            )
            self.api_v1 = tweepy.API(auth, wait_on_rate_limit=True)
            
            logger.info("Twitter API clients initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize Twitter clients: {e}")
            raise
    
    def _init_database(self):
        """Initialize SQLite database for tracking processed tweets"""
        try:
            self.conn = sqlite3.connect('job_bot.db', check_same_thread=False)
            cursor = self.conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    author_username TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT FALSE
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS job_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT UNIQUE,
                    author_username TEXT,
                    author_followers INTEGER,
                    account_age_days INTEGER,
                    tweet_text TEXT,
                    matching_keywords TEXT,
                    created_at TIMESTAMP,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            self.conn.commit()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise
    
    @sleep_and_retry
    @limits(calls=180, period=900)  # 180 calls per 15 minutes (Twitter API limit)
    def search_tweets(self, query: str, max_results: int = 100) -> List[Dict]:
        """Search for tweets with rate limiting"""
        try:
            tweets = self.client.search_recent_tweets(
                query=query,
                max_results=max_results,
                tweet_fields=['created_at', 'author_id', 'public_metrics', 'context_annotations'],
                user_fields=['created_at', 'public_metrics', 'verified'],
                expansions=['author_id']
            )
            
            if not tweets.data:
                return []
            
            # Combine tweet data with user data
            users = {user.id: user for user in tweets.includes.get('users', [])}
            
            results = []
            for tweet in tweets.data:
                user = users.get(tweet.author_id)
                if user:
                    results.append({
                        'tweet': tweet,
                        'user': user
                    })
            
            return results
            
        except Exception as e:
            logger.error(f"Error searching tweets: {e}")
            return []
    
    def is_tweet_processed(self, tweet_id: str) -> bool:
        """Check if tweet has already been processed"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM processed_tweets WHERE tweet_id = ?", (tweet_id,))
        return cursor.fetchone() is not None
    
    def mark_tweet_processed(self, tweet_id: str, author_username: str):
        """Mark tweet as processed"""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO processed_tweets (tweet_id, author_username) VALUES (?, ?)",
            (tweet_id, author_username)
        )
        self.conn.commit()
    
    def calculate_account_age(self, created_at: datetime) -> int:
        """Calculate account age in days"""
        return (datetime.now() - created_at.replace(tzinfo=None)).days
    
    def extract_matching_keywords(self, text: str) -> List[str]:
        """Extract matching keywords from tweet text"""
        text_lower = text.lower()
        matching = []
        
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                matching.append(keyword)
        
        return matching
    
    def has_excluded_words(self, text: str) -> bool:
        """Check if tweet contains excluded words"""
        text_lower = text.lower()
        return any(excluded.lower() in text_lower for excluded in self.excluded_words)
    
    def meets_criteria(self, tweet_data: Dict) -> Optional[JobPost]:
        """Check if tweet meets all criteria"""
        tweet = tweet_data['tweet']
        user = tweet_data['user']
        
        # Check if already processed
        if self.is_tweet_processed(tweet.id):
            return None
        
        # Check tweet age - exclude tweets older than 60 hours
        tweet_age_hours = (datetime.now() - tweet.created_at.replace(tzinfo=None)).total_seconds() / 3600
        if tweet_age_hours > 60:
            logger.info(f"Tweet {tweet.id} excluded: {tweet_age_hours:.1f} hours old (limit: 60 hours)")
            return None
        
        # Extract matching keywords
        matching_keywords = self.extract_matching_keywords(tweet.text)
        if not matching_keywords:
            return None
        
        # Check for excluded words
        if self.has_excluded_words(tweet.text):
            logger.info(f"Tweet {tweet.id} excluded due to negative keywords")
            return None
        
        # Check follower count
        follower_count = user.public_metrics.get('followers_count', 0)
        if follower_count < self.min_followers:
            logger.info(f"Tweet {tweet.id} excluded: only {follower_count} followers")
            return None
        
        # Check account age
        account_age = self.calculate_account_age(user.created_at)
        if account_age < self.min_account_age_days:
            logger.info(f"Tweet {tweet.id} excluded: account only {account_age} days old")
            return None
        
        # Additional quality checks
        if self.is_likely_spam(tweet.text, user):
            logger.info(f"Tweet {tweet.id} excluded: appears to be spam")
            return None
        
        return JobPost(
            tweet_id=tweet.id,
            author_username=user.username,
            author_followers=follower_count,
            account_age_days=account_age,
            tweet_text=tweet.text,
            created_at=tweet.created_at,
            matching_keywords=matching_keywords
        )
    
    def is_likely_spam(self, text: str, user) -> bool:
        """Basic spam detection"""
        # Check for excessive hashtags
        hashtag_count = text.count('#')
        if hashtag_count > 10:
            return True
        
        # Check for excessive mentions
        mention_count = text.count('@')
        if mention_count > 5:
            return True
        
        # Check for suspicious patterns
        spam_patterns = [
            r'click here',
            r'dm me',
            r'follow for follow',
            r'free money',
            r'make \$\d+',
            r'work from home.*\$\d+'
        ]
        
        for pattern in spam_patterns:
            if re.search(pattern, text.lower()):
                return True
        
        return False
    
    def save_job_post(self, job_post: JobPost):
        """Save job post to database"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO job_posts 
            (tweet_id, author_username, author_followers, account_age_days, 
             tweet_text, matching_keywords, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            job_post.tweet_id,
            job_post.author_username,
            job_post.author_followers,
            job_post.account_age_days,
            job_post.tweet_text,
            ','.join(job_post.matching_keywords),
            job_post.created_at
        ))
        self.conn.commit()
    
    def send_notification(self, job_post: JobPost):
        """Send notification about new job post"""
        tweet_url = f"https://twitter.com/{job_post.author_username}/status/{job_post.tweet_id}"
        
        message = f"""🚨 New Frontend Job Alert!
        
👤 @{job_post.author_username} ({job_post.author_followers:,} followers)
📅 Account age: {job_post.account_age_days} days
🔍 Keywords: {', '.join(job_post.matching_keywords)}
🔗 {tweet_url}

"{job_post.tweet_text[:200]}{'...' if len(job_post.tweet_text) > 200 else ''}"
        """
        
        # Try to send DM to yourself
        if self.your_twitter_id:
            try:
                self.client.create_direct_message(
                    dm_conversation_id=self.your_twitter_id,
                    text=message
                )
                logger.info(f"DM notification sent for tweet {job_post.tweet_id}")
            except Exception as e:
                logger.error(f"Failed to send DM: {e}")
        
        # Try webhook notification
        # if self.notification_webhook:
        #     try:
        #         payload = {
        #             'text': message,
        #             'tweet_url': tweet_url,
        #             'job_post': {
        #                 'tweet_id': job_post.tweet_id,
        #                 'author': job_post.author_username,
        #                 'followers': job_post.author_followers,
        #                 'keywords': job_post.matching_keywords
        #             }
        #         }
                
        #         response = requests.post(
        #             self.notification_webhook,
        #             json=payload,
        #             timeout=10
        #         )
        #         response.raise_for_status()
        #         logger.info(f"Webhook notification sent for tweet {job_post.tweet_id}")
                
        #     except Exception as e:
        #         logger.error(f"Failed to send webhook notification: {e}")
    
    def run_search_cycle(self):
        """Run one search cycle"""
        logger.info("Starting search cycle...")
        
        # Build search query
        keyword_query = ' OR '.join([f'"{keyword}"' for keyword in self.keywords])
        query = f"({keyword_query}) -is:retweet lang:en"
        
        try:
            # Search for tweets
            tweet_results = self.search_tweets(query, max_results=100)
            logger.info(f"Found {len(tweet_results)} tweets to process")
            
            new_jobs_found = 0
            
            for tweet_data in tweet_results:
                job_post = self.meets_criteria(tweet_data)
                
                if job_post:
                    # Save to database
                    self.save_job_post(job_post)
                    
                    # Send notification
                    self.send_notification(job_post)
                    
                    # Mark as processed
                    self.mark_tweet_processed(job_post.tweet_id, job_post.author_username)
                    
                    new_jobs_found += 1
                    logger.info(f"New job found: @{job_post.author_username} - {job_post.matching_keywords}")
                else:
                    # Mark as processed even if it doesn't meet criteria
                    tweet = tweet_data['tweet']
                    user = tweet_data['user']
                    self.mark_tweet_processed(tweet.id, user.username)
            
            logger.info(f"Search cycle completed. {new_jobs_found} new jobs found.")
            
        except Exception as e:
            logger.error(f"Error in search cycle: {e}")
    
    def cleanup_old_records(self):
        """Clean up old database records"""
        cursor = self.conn.cursor()
        # Remove processed tweets older than 7 days
        week_ago = datetime.now() - timedelta(days=7)
        cursor.execute(
            "DELETE FROM processed_tweets WHERE processed_at < ?",
            (week_ago,)
        )
        self.conn.commit()
        logger.info("Cleaned up old database records")
    
    def start_monitoring(self):
        """Start the monitoring process"""
        logger.info("Starting Twitter job monitoring bot...")
        
        # Schedule searches every 15 minutes
        schedule.every(15).minutes.do(self.run_search_cycle)
        
        # Schedule cleanup daily
        schedule.every().day.at("02:00").do(self.cleanup_old_records)
        
        # Run initial search
        self.run_search_cycle()
        
        # Keep running
        while True:
            schedule.run_pending()
            time.sleep(60)  # Check every minute

def main():
    """Main function to start the bot"""
    # Validate environment variables
    required_vars = [
        'TWITTER_BEARER_TOKEN',
        'TWITTER_API_KEY',
        'TWITTER_API_SECRET',
        'TWITTER_ACCESS_TOKEN',
        'TWITTER_ACCESS_TOKEN_SECRET'
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        return
    
    try:
        bot = TwitterJobBot()
        bot.start_monitoring()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise

if __name__ == "__main__":
    main()
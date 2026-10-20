import os
import pandas as pd
import pickle
from sklearn.feature_extraction.text import TfidfVectorizer

# 1. Load recipes.csv
df = pd.read_csv('recipes.csv')

# 2. Train TF-IDF Vectorizer
tfidf = TfidfVectorizer(stop_words='english')
tfidf_matrix = tfidf.fit_transform(df['ingredients'].fillna(''))

model_data = {
    'df': df,
    'tfidf': tfidf,
    'tfidf_matrix': tfidf_matrix
}

# 3. Siguraduhing e-exist ang 'models' directory at i-save ang .pkl
os.makedirs('models', exist_ok=True)
with open('models/recipe_similarity_profile.pkl', 'wb') as f:
    pickle.dump(model_data, f)

print("✅ Tagumpay! Na-generate na ang model data sa models/recipe_similarity_profile.pkl")
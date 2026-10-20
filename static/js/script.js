async function askAIChef() {
    const query = document.getElementById('userQuery').value;
    const responseSection = document.getElementById('aiResponseSection');
    const replyText = document.getElementById('aiReplyText');
    const recipesGrid = document.getElementById('aiRecipesGrid');

    if(!query.trim()) {
        alert("Maglagay muna ng tanong o sangkap!");
        return;
    }

    responseSection.style.display = "block";
    replyText.innerText = "Nag-iisip ang AI Chef ng resipi at naghahanda ng gabay sa pagluluto...";
    recipesGrid.innerHTML = "";

    try {
        const response = await fetch('/ask_ai', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query })
        });

        const data = await response.json();
        replyText.innerText = data.reply;
        recipesGrid.innerHTML = "";

        if(data.recipes.length === 0) {
            recipesGrid.innerHTML = '<p style="color:red; text-align:center;">Walang nahanap na resipi.</p>';
            return;
        }

        data.recipes.forEach(recipe => {
            // I-escape o ipasa ang data sa modal nang maayos
            recipesGrid.innerHTML += `
                <div class="result-card" onclick='openRecipeModalFromObj(${JSON.stringify(recipe)})'>
                    <img src="${recipe.image_url}" alt="${recipe.title}">
                    <div class="result-card-body">
                        <h3>${recipe.title}</h3>
                        <span class="budget-badge">💰 ${recipe.budget_level}</span>
                        <p><strong>Sangkap:</strong> ${recipe.ingredients}</p>
                    </div>
                </div>
            `;
        });

    } catch (error) {
        replyText.innerText = "May error sa pag-connect sa server.";
    }
}

// Category Filtering
function filterCategory(category) {
    const cards = document.querySelectorAll('.grid-card');
    const tabs = document.querySelectorAll('.tab-btn');

    tabs.forEach(btn => btn.classList.remove('active'));
    event.target.classList.add('active');

    cards.forEach(card => {
        const cardCat = card.getAttribute('data-category');
        if(category === 'Lahat' || cardCat === category) {
            card.style.display = "block";
        } else {
            card.style.display = "none";
        }
    });
}

// Modal Recipe Reader mula sa Click sa Catalog
function openRecipeModal(title, category, ingredients, instructions, budget, bestFor, imgUrl) {
    // Humanapin ang guide base sa title para sa catalog items
    fetch('/ask_ai', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: title })
    }).then(res => res.json()).then(data => {
        const matched = data.recipes.find(r => r.title === title);
        if(matched) {
            openRecipeModalFromObj(matched);
        }
    });
}

// Modal Reader gamit ang buong recipe object (para sa AI Search results)
function openRecipeModalFromObj(recipe) {
    document.getElementById('modalImg').src = recipe.image_url;
    document.getElementById('modalCategory').innerText = recipe.category;
    document.getElementById('modalTitle').innerText = recipe.title;
    document.getElementById('modalBudget').innerText = recipe.budget_level;
    document.getElementById('modalIngredients').innerText = recipe.ingredients;
    
    // Ilagay ang Chef Expert Guide
    document.getElementById('modalMeasurements').innerText = recipe.cooking_guide.measurements;
    document.getElementById('modalGisa').innerText = recipe.cooking_guide.gisa_time;
    document.getElementById('modalBoil').innerText = recipe.cooking_guide.boil_time;
    document.getElementById('modalTip').innerText = recipe.cooking_guide.secret_tip;

    const instructionsList = document.getElementById('modalInstructions');
    instructionsList.innerHTML = "";
    
    const steps = recipe.instructions.split('\\n');
    steps.forEach(step => {
        if(step.trim() !== "") {
            const li = document.createElement('li');
            li.innerText = step.replace(/^\d+\.\s*/, '');
            instructionsList.appendChild(li);
        }
    });

    document.getElementById('recipeModal').style.display = "flex";
}

function closeRecipeModal() {
    document.getElementById('recipeModal').style.display = "none";
}

window.onclick = function(event) {
    const modal = document.getElementById('recipeModal');
    if (event.target == modal) {
        modal.style.display = "none";
    }
}